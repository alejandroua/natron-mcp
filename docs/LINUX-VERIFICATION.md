# Linux verification — natron-mcp on the fork's Linux AppImage (2026-10-12)

First cross-platform validation of the pack, run on Windows 10 via WSL2. Host:
`Natron-release/Natron-2.6-linux-x86_64.AppImage` (Natron 2.6 @ `515e23da`,
RB-2.6, bundles Python 3.13). Server: venv with Python 3.12.3 (Ubuntu 24.04).
**Result: 34/35 pass** — 13 unit + 9 headless contract + 5 MCP-layer + 7/8 GUI.

## Environment

- WSL2 distro: Ubuntu 24.04 (`wsl --import NatronLinux ...` from Canonical's
  noble WSL rootfs), default user `natrondev`.
- AppImage extracted once (`--appimage-extract`, no FUSE needed) to
  `~/natron-linux/squashfs-root/`.
- Server venv: `~/.natron-mcp-venv` (`pip install -e <workspace>/Natron/tools/mcp/server`).

## How to run

```bash
export NATRON_BIN=~/natron-linux/squashfs-root/usr/bin/NatronRenderer      # headless + MCP-layer suites
export NATRON_GUI_BIN=~/natron-linux/squashfs-root/usr/bin/Natron          # GUI suite
python -m pytest /mnt/c/.../OpenCode_Natron3D/Natron/tools/mcp/tests -v    # unit tests run everywhere
```

GUI tests need a display: `xvfb-run -a -s "-screen 0 1600x1000x24"` with
`LIBGL_ALWAYS_SOFTWARE=1` works (no GPU needed). Do **not** set
`LD_LIBRARY_PATH` to the bundled `usr/lib` — it shadows system libs (breaks
`awk`/`readline` via the bundled `libtinfo.so.6`) and is unnecessary once the
distro packages below are installed.

## Distro packages the AppImage needs (not bundled)

linuxdeploy excluded common system libs; on a fresh Ubuntu 24.04:

```
libsm6 libice6 libxext6 libxrender1 libxrandr2 libxfixes3 libxcursor1
libxinerama1 libxi6 libgl1 libegl1 libopengl0 libglx0 libxkbcommon0
libdbus-1-3 libfontconfig1
```

(`NatronRenderer --version` was the quickest probe; first failure was
`libSM.so.6`, then the GUI needed `libOpenGL.so.0`.)

## Findings

1. **Plugin-count threshold is workspace-specific (test bug, false failure).**
   `test_gui_instance_info` asserts `plugin_count > 1900` — calibrated for the
   Windows workspace build (2348 plugins with community PyPlugs). The AppImage
   ships 562. Threshold should be relative to the host (e.g. `> 500`, or
   configurable via env).
2. **OS-level modal tools stay Windows-only, as designed.**
   `session_modal_status`/`dismiss` degrade to bridge-first with
   `supported: false` from `modal_windows.py`; all GUI tests still passed
   (in-host watchdog covers C++-initiated modals; `project_new` pre-save avoids
   the Python-initiated one). A Linux equivalent (xdotool via X11) is backlog.
3. **`install.py` does not classify AppImage/portable-Linux layouts.**
   Portable-root detection looks for `App\Natron.exe` / `Natron.exe` /
   `Renderer\NatronRenderer.exe`; the AppImage layout is `usr/bin/{Natron,
   NatronRenderer, natron-python}`. Tests were run with env vars instead
   (`resolve_binary()` env overrides work fine). Installer needs a Linux-layout
   branch.
4. **`resolve_binary()` workspace fallbacks are `.exe`-only** (cosmetic; env
   vars are the supported path on Linux).
5. **Workspace-specific WSL note:** the inbox (Win10) `wsl.exe` pre-expands
   `~`/`$VARS` in inline `wsl ... bash -lc '...'` commands, so `NATRON_BIN`
   silently resolved to a `/mnt/c` path and the first GUI run failed with a
   missing-cwd `FileNotFoundError`. Always drive WSL through script files.

## Verdict

The bridge and server are genuinely cross-platform: headless, FastMCP
end-to-end (incl. `sessions_spawn`/`kill`), and GUI contract suites pass
against a real Linux host with zero code changes. Remaining work is the three
fixes above (test threshold, installer layout, optional xdotool modal layer).

## Update (2026-10-12, later the same day)

Findings 1 and 3 fixed; full suite now **35/35** on the AppImage host:

- `test_gui_instance_info` threshold lowered to a host-agnostic sanity floor
  (`> 400`, with per-host calibration in the comment).
- `install.py` recognizes AppImage roots (`<root>/usr/bin/{Natron,
  NatronRenderer}`; helper `app_prefix()` keeps Resources/Plugins under
  `usr/`), `root_of_exe` maps `usr/bin/<exe>` correctly, `register_install`
  records `usr/bin` binaries, remove sweeps `usr/`-layout leftovers, and
  pointing at an `.AppImage` file prints an extract-first hint.
- Verified end-to-end on the extracted AppImage: dry-run -> install ->
  registry (`gui_bin`/`renderer_bin` = `usr/bin/...`) -> **bare `Natron`
  launch autostarts the bridge via the staged `initGui.py`** (session
  registered, mode gui) -> remove (exact, incl. `__pycache__` purge, zero
  leftovers).
- Bridge `get_server()` re-exported from `natron_mcp_bridge/__init__` — the
  generated autoload's idempotency check required it and was broken on all
  platforms until now (only bootstrap-script loading was exercised before).
- Remove now purges `__pycache__` **before** deleting manifest files so the
  emptied bridge dirs collapse (live Windows GUI round-trip: install ->
  autoload session -> stop -> remove, zero leftovers; Linux cycle re-verified).
- Repacked `natron-mcp-0.1.0.zip` (32 files: original 31 + this doc).

