# AmebaPro2 FreeRTOS build pipeline

How the build is wired for the Realtek AmebaPro2 (RTL8735B / AMB82-mini) FreeRTOS SDK,
and how an out-of-tree application and incremental tests plug into it. This is the model
`ameba-flash-ui` drives; understanding it helps when a build target behaves unexpectedly.

## The chain

```
build_test.sh <ID>                 # thin wrapper -> TEST/<ID>/test.cmake
        ▼
build_freertos.sh [TEST/<ID>/test.cmake]
        │  - puts the xPack arm-none-eabi toolchain on PATH
        │  - rm -rf build/  (clean configure every time)
        │  - cmake .. -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake [-DTEST_CMAKE=<abs>]
        │  - cmake --build .             -> application/firmware_ntz.bin  (app image)
        │  - cmake --build . --target flash -> flash_ntz.bin             (full image)
        │  - copies both into images/
        ▼
SDK scenario.cmake                 ← BRANCH POINT
        │  always includes the SDK's own src/main.c (defines main() + a WEAK app_example())
        │
        ├─ if -DTEST_CMAKE is set & exists → include(<TEST_CMAKE>)        # a test
        └─ else                            → include(<your app>.cmake)    # full application
        │
        │  both append to scn_sources / scn_inc_path
        ▼
SDK application.cmake               # compiles scn_sources, adds scn_inc_path, links the image
        ▼
images/flash_ntz.bin  +  images/firmware_ntz.bin
```

## Two `main.c` is normal — not a conflict

Seeing **two** compiled `main.c` in the build log is expected:

- The SDK's `src/main.c` defines `main()` (init + `vTaskStartScheduler`) and a
  **weak** `__weak void app_example(void) {}` stub. It **must** link — it owns `main()`.
- Your code (the full application **or** a `TEST/<id>/main.c`) defines a **strong**
  `app_example()`. The linker takes the strong one; there is no `multiple definition` error.

A real conflict only happens if both the full-application cmake **and** a test are pulled
into one build (two strong `app_example`). That is why tests build through `-DTEST_CMAKE`,
which makes the branch exclusive.

## The `test.cmake` contract

The build does **not** auto-discover sources. Each `test.cmake` must list **every** `.c`
its `app_example()` transitively needs, and **every** header directory. SDK libraries
(FreeRTOS, the I2C/SPI/GPIO HAL, `dbg_printf`) are provided by the SDK — you do not list
them.

```cmake
set(TEST_ROOT "${CMAKE_CURRENT_LIST_DIR}")
set(SRC_ROOT  "${TEST_ROOT}/../../src")

list(APPEND scn_sources
    ${TEST_ROOT}/main.c              # the test's app_example()
    ${SRC_ROOT}/hal/foo.c            # only what this test needs
)
list(APPEND scn_inc_path
    ${SRC_ROOT}
    ${SRC_ROOT}/hal
)
```

**Is the source set complete?** Walk each file's `#include "..."` and confirm every local
`.c` is in `scn_sources` and every header dir is in `scn_inc_path`. Symptoms of an
incomplete set:

- `undefined reference to <func>` at link time → a needed `.c` is missing from `scn_sources`.
- `fatal error: X.h: No such file` at compile time → a directory is missing from `scn_inc_path`.

## Adding an incremental test

1. Create `TEST/NN_<name>/`.
2. Add `main.c` with a single `void app_example(void)` (and `void _fini(void){}`, which
   newlib expects). No `main()` — that lives in the SDK.
3. Add `test.cmake` listing `scn_sources` + `scn_inc_path` (copy a neighbouring test).
4. Build it: `./build_test.sh NN_<name>` → success prints `Build complete — images in ./images/`.
5. Flash and check the serial log.

Isolation rule: if test `N` fails, compare with `N-1` — it differs by exactly one subsystem.

## Artifacts

| Path | What | Tracked |
|---|---|---|
| `sdk/.../GCC-RELEASE/build/` | intermediates, `.axf`, `.map` | no |
| `build/application/firmware_ntz.bin` | app image | no |
| `build/flash_ntz.bin` | full image (target `flash`) | no |
| `images/*.bin` | copies the flasher reads | no |

`build_freertos.sh` does `rm -rf build/` on every run, so the `.axf` for `addr2line`
exists only until the next build — decode any crash dump before rebuilding.

> The full-image CMake target is named **`flash`**, not `flash_ntz`
> (`--target flash_ntz` → `No rule to make target`). A mid-log
> `[ERR]cannot open user binary file user.bin` is benign — the images are still produced.
