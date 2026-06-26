# File & environment hygiene

Rules for keeping an AmebaPro2 FreeRTOS project (and this tool) clean and reproducible.
Written for coding agents driving the build, but they apply to humans too.

## What is versioned vs not

Versioned: your application/test sources, build scripts (`*.sh`, `*.cmake`), docs.

Git-ignored — do not commit or "restore" into git:

- the vendored SDK (`sdk/`) — a separate upstream, cloned/submoduled on its own;
- the toolchain (`toolchain/`) — large, downloaded locally;
- build artifacts: `images/`, any `*.bin` / `*.axf` / `*.map` / `*.asm`, build logs;
- secrets (e.g. `src/secrets.h`);
- `.DS_Store`.

Before `git add`, check `git status`: if `sdk/`, `toolchain/`, `images/`, or `*.bin` want
to be staged, that is a mistake.

## Temporary files

Scratch data, log dumps, `.map` parsing, throwaway scripts → a scratch directory **outside**
the repo, never the project root or a `TEST/` dir. If you created something temporary in the
tree for a one-off check, delete it afterwards.

## Build artifacts

- The only correct home for images is `images/` (populated by `build_freertos.sh`).
- The SDK `build/` dir is recreated (`rm -rf build/`) on every build — keep nothing valuable
  there and do not hand-edit generated files.
- The crash-decode ELF (`application.ntz.axf`) lives only until the next build — decode dumps
  before rebuilding, or copy the `.axf` aside.

## SDK & toolchain — do not edit casually

`sdk/` is vendored upstream. Editing SDK files breaks reproducibility and is lost on update.
Keep build configuration in *your* files (the app cmake, `TEST/*/test.cmake`, `build_*.sh`),
not in the SDK's `application.cmake` / `scenario.cmake`. If an SDK edit is truly required,
record it in a decisions log and the commit message.

## Secrets

- Real credentials go only in a git-ignored file (e.g. `src/secrets.h`), with a committed
  `secrets.h.example` template carrying no real values.
- Config headers hold placeholders only. Do not print secrets to the log or docs.

## Docs — one source of truth

Do not create duplicate docs. Update the existing file instead of copying it next to itself.
New docs get a clear name and a link from the project's entry-point doc so they are findable.

## macOS environment notes

- `timeout` / `gtimeout` are usually **not** installed — do not rely on them in capture
  scripts; use a background process + `kill`, or a serial monitor.
- The board's port name drifts (`...110` ↔ `...1110`) after each reset — always compute it
  (`ls /dev/cu.wchusbserial* | tail -1`), never hard-code it.
- On Apple Silicon the flasher is `uartfwburn.arm.darwin` (selected via `uname -m`).
- Free the port before flashing or monitoring — close `screen`/`picocom` first.

## Flashing needs a human

The agent does not press buttons. Before flashing, ask the user to put the board into UART
DOWNLOAD mode and wait for confirmation; after `download success`, ask them to press RESET.
Do not loop the flash or step the baud down in a loop — the flasher already sweeps.
