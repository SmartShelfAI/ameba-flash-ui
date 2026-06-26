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
}


def burn_binary():
    name = "uartfwburn.arm.darwin" if os.uname().machine == "arm64" else "uartfwburn.darwin"
    return os.path.join(PG_DIR, name)


def find_port():
    ports = sorted(glob.glob(PORT_GLOB))
    return ports[-1] if ports else None


def port_is_free(port):
    """True if the tty can be opened (not held by screen/picocom/flasher)."""
    try:
        fd = os.open(port, os.O_RDWR | os.O_NONBLOCK)
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
_ERR_RE = re.compile(r"error:|undefined reference|No rule to make|\bfatal\b|\*\*\* ", re.I)


def stream_build(wfile, cmd, cwd):
    """Run a build. Stream ONLY progress + error lines (not the full log).

    A full SDK build prints tens of thousands of lines; forwarding them all
    crashes the browser tab and is useless here (the user watches Serial, not
    the build log). We emit:
      - 'progress' events when the cmake/make percentage changes,
      - 'line' events only for error-looking lines (capped),
      - on failure, the last ~30 lines so the cause is visible,
      - a final 'done' with the exit code.
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
                        return
                continue
            if _ERR_RE.search(line) and err_sent < ERR_CAP:
                err_sent += 1
                sse_send(wfile, "line", {"text": line})
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    ok = proc.returncode == 0
    if not ok:
        sse_send(wfile, "line", {"text": "--- last build lines ---"})
        for l in tail:
            sse_send(wfile, "line", {"text": l})
    sse_send(wfile, "done", {"code": proc.returncode, "ok": ok})


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
        if u.path == "/api/build":
            return self.api_build(parse_qs(u.query))
        if u.path == "/api/flash":
            return self.api_flash(parse_qs(u.query))
        if u.path == "/api/serial":
            return self.api_serial()
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
            stream_build(self.wfile, cmd, ROOT)
        finally:
            STATE["lock"].release()

    def api_flash(self, q):
        mode = q.get("mode", ["full"])[0]
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
            if mode == "app":
                img = os.path.join(IMAGES, "firmware_ntz.bin")
                attempts = [("115200", "1", ["-s", "0x60000"])]
            else:
                img = os.path.join(IMAGES, "flash_ntz.bin")
                # same baud sweep as flash.sh: 2M -> 921600 -> 115200
                attempts = [("2000000", "32", ["-U"]), ("921600", "8", ["-U"]), ("115200", "1", ["-U"])]
            if not os.path.exists(img):
                sse_send(self.wfile, "done", {"code": -1, "ok": False,
                         "msg": "Image not found — build it first (%s)." % os.path.basename(img)})
                return
            ok = False
            for baud, x, extra in attempts:
                cmd = [burn, "-p", port, "-f", img, "-b", baud] + extra + ["-x", x]
                sse_send(self.wfile, "line", {"text": ">>> attempt baud=%s -x %s" % (baud, x)})
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True)
                for line in proc.stdout:
                    if not sse_send(self.wfile, "line", {"text": line.rstrip()}):
                        proc.kill()
                        return
                proc.wait()
                if proc.returncode == 0:
                    ok = True
                    break
                sse_send(self.wfile, "line", {"text": ">>> baud=%s failed, trying slower..." % baud})
            sse_send(self.wfile, "done", {
                "code": 0 if ok else 1, "ok": ok,
                "msg": "DONE. Now press RESET on the board manually — the AMB82-mini does not reset itself."
                       if ok else "Flashing failed. Make sure the board is in UART DOWNLOAD mode and retry.",
            })
        finally:
            STATE["lock"].release()

    def api_serial(self):
        if STATE["serial_active"]:
            self._sse_headers()
            sse_send(self.wfile, "done", {"ok": False, "msg": "Serial is already being read."})
            return
        port = find_port()
        if not port:
            self._sse_headers()
            sse_send(self.wfile, "done", {"ok": False, "msg": "Port not found."})
            return
        os.makedirs(LOGDIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        logpath = os.path.join(LOGDIR, "serial_%s.log" % stamp)

        stop = threading.Event()
        STATE["serial_active"] = True
        STATE["serial_stop"] = stop
        self._sse_headers()
        sse_send(self.wfile, "line", {"text": "[serial] %s @115200 -> %s" % (port, logpath)})
        try:
            fd = os.open(port, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
            configure_tty(fd, 115200)   # raw 8N1 @115200 on this fd (fixes garbage)
        except OSError as e:
            STATE["serial_active"] = False
            sse_send(self.wfile, "done", {"ok": False, "msg": "Cannot open port: %s" % e})
            return
        try:
            with os.fdopen(fd, "rb", buffering=0) as f, open(logpath, "ab") as log:
                buf = b""
                while not stop.is_set():
                    r, _, _ = select.select([f], [], [], 0.5)
                    if not r:
                        continue
                    chunk = f.read(4096)
                    if not chunk:
                        continue
                    log.write(chunk)
                    log.flush()
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").rstrip("\r")
                        if not sse_send(self.wfile, "line", {"text": text}):
                            stop.set()
                            break
        finally:
            STATE["serial_active"] = False
            STATE["serial_stop"] = None
        sse_send(self.wfile, "done", {"ok": True, "msg": "Serial stopped. Log: %s" % logpath})

    def api_serial_stop(self):
        ev = STATE.get("serial_stop")
        if ev:
            ev.set()
        self._json({"stopped": True})

    def api_save_log(self):
        # Save the current output panel text into <PROJECT_ROOT>/LOG/ with a
        # timestamped filename. ROOT is the folder the build runs from.
        body = self._read_body()
        text = body.get("text", "")
        if not text.strip():
            return self._json({"ok": False, "msg": "nothing to save"}, 400)
        save_dir = os.path.join(ROOT, "LOG")
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
