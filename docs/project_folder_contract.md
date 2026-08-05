# Project folder contract — what the panel accepts for Build / Flash

Formal requirement for a folder selected via **Browse…** (`POST /api/project`).
The panel validates the folder once at selection time and then derives all
paths from it at call time (`ROOT`).

## A. Buildable project (`can_build: true`) — Build + Flash + Serial enabled

A folder qualifies as a **buildable project** iff marker **A1** exists.
Everything else is optional capability, discovered from the layout:

| # | Required path | Meaning |
|---|---|---|
| A1 | `<root>/build_freertos.sh` | **Marker (mandatory).** Entry point for the full-application build. Run as a subprocess with `cwd=<root>`; exit code 0 = success; `[ N%]` lines in stdout are parsed as progress. |
| A2 | `<root>/build_test.sh` | Enables incremental-test targets. Run as `build_test.sh <id>` with `cwd=<root>`, same progress/exit-code contract. |
| A3 | `<root>/TEST/<id>/test.cmake` | One build target per such `<id>` (listed by `GET /api/targets`). The test's sources (e.g. `main.c` with a strong `app_example()`) live in the same folder; the CMake contract is described in [`build_pipeline.md`](build_pipeline.md). |
| A4 | `<root>/images/flash_ntz.bin` | Build output, full image (flashed in `full` mode). |
| A5 | `<root>/images/firmware_ntz.bin` | Build output, app-only image (flashed in `app` mode at `0x60000`). |
| A6 | `<root>/sdk/tools/Pro2_PG_tool _v1.4.3/uartfwburn[.arm].darwin` | Flasher binary, picked by `uname -m`. If absent, the panel falls back to the copy in the tool's home project (`HOME_PG_DIR`). |

## B. Flash-only folder (`can_build: false`) — Flash + Serial only, Build disabled

Accepted iff it contains **no** `build_freertos.sh` but at least one file
matching `flash_ntz*.bin` or `firmware_ntz*.bin` in any of:

- `<root>/` itself,
- `<root>/images/`,
- one level down: `<root>/<build>/` (build collections, e.g. `Free_RTOS/positioning_v1_bridge/`).

Versioned names are expected (`flash_ntz_<build>_v1.bin`). When several
candidates of the same kind exist, **the newest mtime wins**; the exact
resolved file is always shown in the image-status line before flashing.

## Rejected

Any other folder is rejected with an error naming the received path. In
particular, a folder holding **only `.bin` images is not buildable**: a `.bin`
is the compiler's output, not an input — there are no sources, no CMake glue
and no SDK to compile from.

## Notes

- The serial log works against any accepted folder; per-target logs go to
  `TEST/<id>/LOG/`, otherwise to `<root>/LOG/`.
- Switching the folder is persisted to `state.json` next to `serve.py` and
  restored on the next start.
