#!/usr/bin/env python3
"""
Offline local control panel for the AMB82-mini (AmebaPro2 / RTL8735B) FreeRTOS build.

Zero external dependencies — Python 3 standard library only.
Serves a small web UI (index.html) and shells out to the existing
build_*.sh scripts and the uartfwburn flasher. Everything runs on
127.0.0.1, no internet.

Run from your AmebaPro2 project root (the dir with build_freertos.sh / images/):
    PROJECT_ROOT=/path/to/your/ameba-project python3 /path/to/serve.py
    # or just `python3 serve.py` if it sits in the project root
    # then open http://127.0.0.1:8765 in a browser

IMPORTANT: this tool CANNOT press the board buttons. The UI prompts the human to
enter UART DOWNLOAD mode before flashing and to press RESET afterwards — the
board does not reset itself.
"""

import collections
import glob
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── Paths ────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
# Project root = where build_freertos.sh / build_test.sh / images/ live.
# Defaults to the current working directory; override with PROJECT_ROOT=/abs/path.
ROOT = os.path.abspath(os.environ.get("PROJECT_ROOT") or os.getcwd())
IMAGES = os.path.join(ROOT, "images")
LOGDIR = os.path.join(ROOT, "logs")
PG_DIR = os.path.join(ROOT, "sdk", "tools", "Pro2_PG_tool _v1.4.3")
PORT_GLOB = "/dev/cu.wchusbserial*"
HTTP_PORT = int(os.environ.get("FLASH_UI_PORT", "8765"))

# ── Shared state (one build/flash at a time; serial vs flash are exclusive) ───
STATE = {
    "lock": threading.Lock(),     # guards "busy" op (build OR flash)
    "serial_active": False,       # a serial reader is holding the port
    "serial_stop": None,          # threading.Event to stop the reader
    "session_counter": 0,         # increments per auto-log serial session
}

# Auto-log rotation: start a new numbered part when the current one exceeds this.
ROTATE_BYTES = 10 * 1024 * 1024   # 10 MB


class RotatingWriter:
    """Append-only writer that rolls over to a new numbered part by size.

    rotate=False -> a single "<base>.log".
    rotate=True  -> "<base>_part01.log", "<base>_part02.log", ... rolling at `limit`.
    """
    def __init__(self, base, rotate, limit):
        self.base, self.rotate, self.limit = base, rotate, limit
        self.part, self.written, self.f, self.path = 1, 0, None, None
        self._open()

    def _open(self):
        self.path = ("%s_part%02d.log" % (self.base, self.part)) if self.rotate else (self.base + ".log")
        self.f = open(self.path, "ab")

    def write(self, chunk):
        if self.rotate and self.written and self.written + len(chunk) > self.limit:
            self.f.close()
            self.part += 1
            self.written = 0
            self._open()
        self.f.write(chunk)
        self.f.flush()
        self.written += len(chunk)

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def burn_binary():
    name = "uartfwburn.arm.darwin" if os.uname().machine == "arm64" else "uartfwburn.darwin"
    return os.path.join(PG_DIR, name)


def find_port():
    ports = sorted(glob.glob(PORT_GLOB))
    return ports[-1] if ports else None


def target_log_dir(target):
    """LOG/ folder for a build target — logs land next to what was compiled.

    'full' / empty / unknown -> <ROOT>/LOG
    a valid test id          -> <ROOT>/TEST/<id>/LOG
    Only an existing TEST/<id> (with test.cmake) is accepted, which also blocks
    path traversal from the client-supplied target.
    """
    if target and target != "full":
        d = os.path.join(ROOT, "TEST", target)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "test.cmake")):
            return os.path.join(d, "LOG")
    return os.path.join(ROOT, "LOG")


def target_dir(target):
    """The folder of a build target — TEST/<id> for a valid test, else <ROOT>.

    Built images are also copied here so each target keeps its own flashable
    image (TEST/<id>/flash_ntz.bin) instead of only the shared images/ copy,
    which a later build of another target would overwrite. Validation against an
    existing test dir blocks path traversal from the client-supplied target.
    """
    if target and target != "full":
        d = os.path.join(ROOT, "TEST", target)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "test.cmake")):
            return d
    return ROOT


def resolve_image(target, basename):
    """Path of the image to flash for a target: prefer the target's own copy,
    fall back to the shared images/ copy."""
    if target and target != "full":
        cand = os.path.join(target_dir(target), basename)
        if os.path.exists(cand):
            return cand
    return os.path.join(IMAGES, basename)


def now_hms_ms():
    """Local wall-clock 'HH:MM:SS.mmm' for serial line timestamps."""
    t = time.time()
    lt = time.localtime(t)
    return "%02d:%02d:%02d.%03d" % (lt.tm_hour, lt.tm_min, lt.tm_sec, int((t % 1) * 1000))


def port_is_free(port):
    """True if the tty can be opened (not held by screen/picocom/flasher)."""
    try:
        fd = os.open(port, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        return True
    except OSError:
        return False


def flush_port(port):
    """Drain any stale bytes sitting in the tty buffers before flashing.

    A leftover boot-log tail or bytes from a previous serial session can confuse
    the AmebaPro2 ROM 'ucfg' handshake. Flushing both directions is cheap and
    harmless. Returns True if the port could be opened+flushed.
    """
    try:
        fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            termios.tcflush(fd, termios.TCIOFLUSH)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def configure_tty(fd, baud=115200):
    """Put the tty into raw 8N1 at `baud` ON THE FD WE READ FROM.

    Setting the baud with an external `stty` then re-opening the device does not
    reliably stick on macOS (the re-open resets termios) — which shows up as
    garbage bytes. Configuring termios on the same fd fixes it.
    """
    speed = getattr(termios, "B%d" % baud, termios.B115200)
    a = termios.tcgetattr(fd)  # [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
    a[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP |
             termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
    a[1] &= ~termios.OPOST
    a[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    a[2] &= ~(termios.CSIZE | termios.PARENB)
    a[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
    a[4] = speed
    a[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, a)


# ── SSE helper ───────────────────────────────────────────────────────────────
def sse_send(wfile, event, payload):
    """Write one Server-Sent-Event. Returns False if the client is gone."""
    try:
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(payload))
        wfile.write(msg.encode("utf-8"))
        wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, ValueError):
        return False


_PCT_RE = re.compile(r"\[\s*(\d+)%\]")
# Real compiler/linker/make diagnostics — not "error:" inside a printf string.
# gcc prints "file:line:col: error:"; the leading ": " avoids matching string literals.
_ERR_RE = re.compile(r": error:|: fatal error:|undefined reference|No rule to make target|\*\*\* \[|recipe for target", re.I)


def stream_build(wfile, cmd, cwd):
    """Run a build. Stream ONLY progress + error lines (not the full log).

    A full SDK build prints tens of thousands of lines; forwarding them all
    crashes the browser tab and is useless here (the user watches Serial, not
    the build log). We emit:
      - 'progress' events when the cmake/make percentage changes,
      - 'line' events only for error-looking lines (capped),
      - on failure, the last ~30 lines so the cause is visible.
    Returns the process exit code, or None if the client disconnected mid-build.
    The caller emits the final 'done' (after any post-build image copy).
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, universal_newlines=True,
    )
    last_pct = -1
    err_sent = 0
    ERR_CAP = 100
    tail = collections.deque(maxlen=30)
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            m = _PCT_RE.search(line)
            if m:
                p = int(m.group(1))
                if p != last_pct:
                    last_pct = p
                    if not sse_send(wfile, "progress", {"pct": p}):
                        proc.kill()
                        return None
                continue
            if _ERR_RE.search(line) and err_sent < ERR_CAP:
                err_sent += 1
                sse_send(wfile, "line", {"text": line})
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        sse_send(wfile, "line", {"text": "--- last build lines ---"})
        for l in tail:
            sse_send(wfile, "line", {"text": l})
    return proc.returncode


# ── Request handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    # -- helpers --
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _serve_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routing --
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._serve_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        if u.path == "/api/targets":
            return self.api_targets()
        if u.path == "/api/uart":
            return self.api_uart()
        if u.path == "/api/image-status":
            return self.api_image_status(parse_qs(u.query))
        if u.path == "/api/build":
            return self.api_build(parse_qs(u.query))
        if u.path == "/api/flash":
            return self.api_flash(parse_qs(u.query))
        if u.path == "/api/serial":
            return self.api_serial(parse_qs(u.query))
        return self._json({"error": "unknown route"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/serial/stop":
            return self.api_serial_stop()
        if u.path == "/api/save-log":
            return self.api_save_log()
        return self._json({"error": "unknown route"}, 404)

    # -- endpoints --
    def api_targets(self):
        targets = [{"id": "full", "label": "Full application (build_freertos.sh)"}]
        test_dir = os.path.join(ROOT, "TEST")
        for name in sorted(os.listdir(test_dir)) if os.path.isdir(test_dir) else []:
            d = os.path.join(test_dir, name)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "test.cmake")):
                targets.append({"id": name, "label": "TEST/" + name})
        self._json({"targets": targets})

    def api_image_status(self, q):
        # Is there an already-built image to flash for this target/mode?
        target = q.get("target", ["full"])[0]
        mode = q.get("mode", ["full"])[0]
        basename = "firmware_ntz.bin" if mode == "app" else "flash_ntz.bin"
        path = resolve_image(target, basename)
        if os.path.exists(path):
            built = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
            self._json({"exists": True, "rel": os.path.relpath(path, ROOT), "built": built})
        else:
            self._json({"exists": False, "rel": os.path.relpath(path, ROOT)})

    def api_uart(self):
        port = find_port()
        if not port:
            return self._json({"available": False, "reason": "port /dev/cu.wchusbserial* not found"})
        free = port_is_free(port)
        self._json({
            "available": True, "port": port, "free": free,
            "reason": "ready to flash" if free else "port busy (close screen/picocom/log)",
        })

    def api_build(self, q):
        target = (q.get("target", ["full"])[0])
        if STATE["serial_active"]:
            self._sse_headers()
            sse_send(self.wfile, "done", {"code": -1, "ok": False,
                     "msg": "Stop the serial log first (the port is busy)."})
            return
        if not STATE["lock"].acquire(blocking=False):
            self._sse_headers()
            sse_send(self.wfile, "done", {"code": -1, "ok": False, "msg": "A build/flash is already running."})
            return
        try:
            if target == "full":
                cmd = [os.path.join(ROOT, "build_freertos.sh")]
            else:
                cmd = [os.path.join(ROOT, "build_test.sh"), target]
            self._sse_headers()
            rc = stream_build(self.wfile, cmd, ROOT)
            if rc is None:
                return  # client disconnected mid-build
            # On success, copy the freshly built images into the target's own
            # folder so each target keeps a flashable copy (images/ gets
            # overwritten by the next build of a different target).
            if rc == 0 and target != "full":
                tdir = target_dir(target)
                if tdir != ROOT:
                    saved = []
                    for fn in ("flash_ntz.bin", "firmware_ntz.bin"):
                        src = os.path.join(IMAGES, fn)
                        if os.path.exists(src):
                            try:
                                shutil.copy2(src, os.path.join(tdir, fn))
                                saved.append(os.path.relpath(os.path.join(tdir, fn), ROOT))
                            except OSError:
                                pass
                    if saved:
                        sse_send(self.wfile, "line", {"text": "saved image → " + ", ".join(saved)})
            sse_send(self.wfile, "done", {"code": rc, "ok": rc == 0})
        finally:
            STATE["lock"].release()

    def api_flash(self, q):
        mode = q.get("mode", ["full"])[0]
        target = q.get("target", ["full"])[0]
        if STATE["serial_active"]:
            self._sse_headers()
            sse_send(self.wfile, "done", {"code": -1, "ok": False,
                     "msg": "Stop the serial log first (the flasher needs the port)."})
            return
        port = find_port()
        burn = burn_binary()
        if not port or not os.path.exists(burn):
            self._sse_headers()
            sse_send(self.wfile, "done", {"code": -1, "ok": False,
                     "msg": "No port, or the flasher binary was not found."})
            return
        if not STATE["lock"].acquire(blocking=False):
            self._sse_headers()
            sse_send(self.wfile, "done", {"code": -1, "ok": False, "msg": "A build/flash is already running."})
            return
        try:
            self._sse_headers()

            # NOTE: we do NOT open/flush the port here before flashing — opening a
            # cu.* device toggles DTR/RTS and can disturb a board sitting in UART
            # download mode. uartfwburn opens it itself; if the port is genuinely
            # held, its "ping fail" output is caught by the diagnosis below.
            baud_choice = q.get("baud", ["auto"])[0]
            if mode == "app":
                basename = "firmware_ntz.bin"
                base_extra = ["-s", "0x60000"]
                sweep = ["115200"]                       # partial app is 115200 only
            else:
                basename = "flash_ntz.bin"
                base_extra = ["-U"]
                sweep = ["2000000", "921600", "115200"]  # fast -> reliable

            # Flash the selected target's own image if it has one, else images/.
            img = resolve_image(target, basename)

            # A specific baud = ONE clean attempt. The auto sweep can poison the
            # ROM session (each failed high-baud attempt wedges it), so on a flaky
            # CH340G picking 115200 explicitly is the reliable path.
            if baud_choice != "auto":
                sweep = [baud_choice]

            def x_for(b):
                b = int(b)
                return "32" if b >= 1000000 else ("8" if b >= 460800 else "1")
            attempts = [(b, x_for(b), base_extra) for b in sweep]

            if not os.path.exists(img):
                sse_send(self.wfile, "done", {"code": -1, "ok": False,
                         "msg": "Image not found — build it first (%s)." % os.path.basename(img)})
                return
            sse_send(self.wfile, "line", {"text": "image: %s" % os.path.relpath(img, ROOT)})
            ok = False
            seen = []   # lowercased flasher output, for token-based diagnosis
            for i, (baud, x, extra) in enumerate(attempts):
                cmd = [burn, "-p", port, "-f", img, "-b", baud] + extra + ["-x", x]
                sse_send(self.wfile, "line", {"text": ">>> attempt baud=%s -x %s" % (baud, x)})
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True)
                for line in proc.stdout:
                    seen.append(line.lower())
                    if not sse_send(self.wfile, "line", {"text": line.rstrip()}):
                        proc.kill()
                        return
                proc.wait()
                if proc.returncode == 0:
                    ok = True
                    break
                if i < len(attempts) - 1:
                    sse_send(self.wfile, "line", {"text": ">>> baud=%s failed, trying slower..." % baud})

            if ok:
                msg = "DONE. Now press RESET on the board manually — the AMB82-mini does not reset itself."
            else:
                blob = " ".join(seen)
                if "ping ok" in blob and ("ucfg fail" in blob or "download fail" in blob):
                    # Board IS in download mode, but the ROM/adapter handshake failed.
                    rate = "non-standard rate" in blob
                    swept = "trying slower" in blob
                    msg = ("Board IS in download mode (ping ok) but the download handshake failed "
                           "(ucfg/download fail). " +
                           ("Your CH340G reported 'non-standard rate' — it can't hold the high bauds. " if rate else "") +
                           ("You used the Auto sweep: each failed attempt wedges the ROM, so 115200 never "
                            "got a clean try. Set Baud = 115200 (single attempt). " if swept
                            else "Set Baud = 115200 (single attempt) if you haven't. ") +
                           "The ROM accepts ONE attempt per download-mode entry, so re-enter download mode "
                           "(RESET + UART DOWNLOAD together / reboot) before EACH retry. If it stays wedged, "
                           "POWER-CYCLE the USB adapter AND the board (replug USB + reboot) to re-enumerate "
                           "the CH340G — often the only fix.")
                elif ("ping fail" in blob or "open fail" in blob or "boot fail" in blob):
                    msg = ("Board did NOT respond (ping fail) — it is not in UART DOWNLOAD mode, or the "
                           "port is busy. Re-enter download mode (hold UART DOWNLOAD, tap RESET, "
                           "release) and retry.")
                else:
                    msg = "Flashing failed. Re-enter UART DOWNLOAD mode (and replug USB if it persists), then retry."
            sse_send(self.wfile, "done", {"code": 0 if ok else 1, "ok": ok, "msg": msg})
        finally:
            STATE["lock"].release()

    def api_serial(self, q=None):
        q = q or {}
        autolog = q.get("autolog", ["0"])[0] == "1"
        ts_on = q.get("ts", ["0"])[0] == "1"   # prefix each line with HH:MM:SS.mmm
        if STATE["serial_active"]:
            self._sse_headers()
            sse_send(self.wfile, "done", {"ok": False, "msg": "Serial is already being read."})
            return
        port = find_port()
        if not port:
            self._sse_headers()
            sse_send(self.wfile, "done", {"ok": False, "msg": "Port not found."})
            return

        stamp = time.strftime("%Y%m%d_%H%M%S")
        if autolog:
            # Full session log into the selected target's LOG/, rotated by size.
            save_dir = target_log_dir(q.get("target", ["full"])[0])
            os.makedirs(save_dir, exist_ok=True)
            STATE["session_counter"] += 1
            sess = STATE["session_counter"]
            base = os.path.join(save_dir, "session%02d_%s" % (sess, stamp))
            rotate = True
            banner = "[serial] %s @115200 -> %s_part01.log (auto-log session %02d, rotating @%dMB)" % (
                port, base, sess, ROTATE_BYTES // (1024 * 1024))
        else:
            os.makedirs(LOGDIR, exist_ok=True)
            base = os.path.join(LOGDIR, "serial_%s" % stamp)
            rotate = False
            banner = "[serial] %s @115200 -> %s.log" % (port, base)

        stop = threading.Event()
        STATE["serial_active"] = True
        STATE["serial_stop"] = stop
        self._sse_headers()
        sse_send(self.wfile, "line", {"text": banner})
        try:
            fd = os.open(port, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
            configure_tty(fd, 115200)   # raw 8N1 @115200 on this fd (fixes garbage)
        except OSError as e:
            STATE["serial_active"] = False
            sse_send(self.wfile, "done", {"ok": False, "msg": "Cannot open port: %s" % e})
            return
        writer = None
        try:
            with os.fdopen(fd, "rb", buffering=0) as f, RotatingWriter(base, rotate, ROTATE_BYTES) as writer:
                buf = b""
                while not stop.is_set():
                    r, _, _ = select.select([f], [], [], 0.5)
                    if not r:
                        continue
                    chunk = f.read(4096)
                    if not chunk:
                        continue
                    if not ts_on:
                        writer.write(chunk)          # raw bytes to the log file
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").rstrip("\r")
                        if ts_on:
                            text = "[%s] %s" % (now_hms_ms(), text)
                            writer.write((text + "\n").encode("utf-8"))  # timestamped to file too
                        if not sse_send(self.wfile, "line", {"text": text}):
                            stop.set()
                            break
        finally:
            STATE["serial_active"] = False
            STATE["serial_stop"] = None
        final = writer.path if writer else (base + ".log")
        parts = (" (%d parts)" % writer.part) if (writer and writer.part > 1) else ""
        sse_send(self.wfile, "done", {"ok": True, "msg": "Serial stopped. Log: %s%s" % (final, parts)})

    def api_serial_stop(self):
        ev = STATE.get("serial_stop")
        if ev:
            ev.set()
        self._json({"stopped": True})

    def api_save_log(self):
        # Save the current output panel text into the selected target's LOG/
        # folder (TEST/<id>/LOG, or <ROOT>/LOG for the full app), timestamped.
        body = self._read_body()
        text = body.get("text", "")
        if not text.strip():
            return self._json({"ok": False, "msg": "nothing to save"}, 400)
        save_dir = target_log_dir(body.get("target", "full"))
        try:
            os.makedirs(save_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(save_dir, "log_%s.txt" % stamp)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return self._json({"ok": False, "msg": "write failed: %s" % e}, 500)
        self._json({"ok": True, "path": path})


def main():
    if not os.path.isdir(ROOT):
        print("Project root not found:", ROOT)
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    print("Flash UI: http://127.0.0.1:%d  (project: %s)" % (HTTP_PORT, ROOT))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
