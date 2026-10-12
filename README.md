# natron-mcp — MCP access to Natron for AI agents

Standalone implementation of the plan in [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md)
(rev 2; M0 + M1 complete). Two components, cross-platform pure Python:

- **`bridge/natron_mcp_bridge/`** — runs *inside* Natron (GUI, NatronRenderer,
  or natron-python). TCP server (127.0.0.1), NDJSON wire protocol, op
  dispatcher, AST safety gate, audit log, session registry. All ops execute on
  Natron's main thread (Qt dispatcher in GUI mode; op-queue drain loop in
  headless mode).
- **`server/natron_mcp/`** — the MCP server (FastMCP). Runs in its own Python
  (>= 3.10), discovers running Natron sessions via `~/.Natron/natron-mcp/sessions.json`,
  forwards tools to the bridge. stdio transport by default, `--http` for
  streamable HTTP on 127.0.0.1:8765.

Works with any Natron 2.6 build exposing the Python API — verified against the
[Niik-l fork](https://github.com/Niik-l/Natron) on Windows (portable build) and
Linux (AppImage); see [`docs/LINUX-VERIFICATION.md`](docs/LINUX-VERIFICATION.md).

## Installing into a Natron (portable install or build tree)

`install.py` (plus `install.bat` / `install.sh` shims) packages natron-mcp for
distribution: clone this repo (or unzip a release) and run the installer.

```powershell
python install.py                     # interactive: asks where Natron lives
python install.py --natron "D:\Natron-2.6" --action install --yes
python install.py --natron "D:\Natron-2.6" --action remove  --yes
python install.py --with-server       # also create the MCP server venv
python install.py --dry-run           # preview
```

The target is auto-classified:

- **Portable Natron install** (dir containing `App\Natron.exe`, `Natron.exe`,
  `Renderer\NatronRenderer.exe`, or an *extracted AppImage* with
  `usr\bin\Natron`):
  - bridge staged to `<root>\Resources\natron-mcp\bridge\` (AppImage layout:
    `<root>\usr\Resources\natron-mcp\bridge\`)
  - autoload at `<root>\Plugins\PyPlugs\natron-mcp\{init.py,initGui.py}` —
    Natron runs these itself (`init.py` in all modes, `initGui.py` in GUI
    sessions) and adds the folder to `sys.path`, so the generated scripts
    self-locate the bridge at runtime: **the install stays relocatable**
  - `Resources\natron-mcp\manifest.json` records everything installed (bridge,
    autoload, and the server venv), so **remove** (and repair = remove + install)
    is exact and leaves the rest of the install untouched; remove also deletes
    the `<root>\natron-mcp\server-venv`, while **repair keeps it** so the server
    is not re-downloaded
  - writes `~/.Natron/natron-mcp/{bridge-dir.txt,install.json}` so an
    independently pip-installed MCP server can find the staged bridge and
    binaries for `sessions_spawn`
- **Fork build tree** (dir containing `tools/win-build/06-install.sh`):
  - injects a marker-delimited staging block into `06-install.sh` (idempotent;
    strip via `--action remove` restores the file byte-identical)
  - generates relocatable autoload scripts into `installer-assets/`
    that the build then stages into every new install
  - if `<tree>\Natron-install` exists, offers to install into it directly

Detection: running the installer again offers install / remove / repair based
on the manifest (absent / partial / installed). Natron must not be running
while installing or removing.

### Which parts do I need?

natron-mcp has two halves and you need both:

- the **bridge** (installed into Natron by this installer — always required), and
- the **MCP server** (a separate Python process your AI client actually
  connects to — required unless you never use AI clients).

The installer decides `--with-server` for you unless you force it:

- a usable server already found (server-venv in the Natron root, `natron-mcp`
  on PATH, or `import natron_mcp` in any Python) → skipped, the existing one
  is used and printed as the client config;
- no server found → offered interactively (default: yes), and in unattended
  mode (`--yes`) it is created automatically;
- `--with-server` / `--no-server` force either behavior.

## Quick start (headless)

```powershell
# 1. start a headless Natron with the bridge (stays alive serving ops)
$env:NATRON_MCP_PORT = "0"    # 0 = auto-pick, written to the session registry
D:\Natron-2.6\Renderer\NatronRenderer.exe bridge\natron_mcp_bridge\bootstrap_headless.py
# (extracted AppImage: squashfs-root/usr/bin/NatronRenderer, same script arg)

# 2. MCP server for your client (separate terminal, server venv)
D:\Natron-2.6\natron-mcp\server-venv\Scripts\natron-mcp.exe          # stdio
D:\Natron-2.6\natron-mcp\server-venv\Scripts\natron-mcp.exe --http   # or HTTP
# or run straight from the repo:
pip install -e server; natron-mcp

# 3. tests (contract tests run a real headless Natron end-to-end)
$env:NATRON_BIN = "D:\Natron-2.6\Renderer\NatronRenderer.exe"
pip install pytest; python -m pytest tests -v
```

## Quick start (GUI) — verified by tests

Launch GUI Natron with the bridge bootstrap (or autoload via initGui.py,
see `bridge/initGui-snippet.py`):

```powershell
$env:NATRON_MCP_PORT = "0"; $env:NATRON_MCP_BRIDGE_DIR = "<repo>\bridge"
Natron.exe bridge\natron_mcp_bridge\bootstrap_gui.py
```

The GUI session registers as `mode: "gui"` in the session registry; the same
MCP server and tools work unchanged. `test_contract_gui.py` runs 8 live tests
against a real GUI session (allow ~40 s: Natron needs 15-25 s to settle before
graph mutations are safe).

Add to `~/.Natron/initGui.py` (see `bridge/initGui-snippet.py`):

```python
import sys
sys.path.insert(0, r"<path-to-this-repo>\bridge")
import natron_mcp_bridge as mcp
try:
    mcp.bind_app(app)
except NameError:
    pass
_srv = mcp.start_gui()          # registers the session, prints the port
print("natron-mcp bridge on port", _srv.port)
```

## Client configuration

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "natron": {
      "command": "C:\\...\\natron-mcp\\server\\.venv\\Scripts\\natron-mcp.exe"
    }
  }
}
```

Claude Code (`.claude/mcp.json`): `{"natron": {"command": "C:\\...\\natron-mcp.exe"}}`.
For the HTTP transport point any MCP client at `http://127.0.0.1:8765/mcp`.

## Tool surface (Phase-1 lean core, 26 tools + M1 session ops)

`sessions_list`, `session_info`, `project_info`, `project_set_settings`,
`project_save/load/new`, `nodes_list`, `node_get`, `node_create`,
`node_create_reader`, `node_create_writer`, `node_delete`, `node_connect`,
`node_disconnect`, `node_position_set`, `knobs_get`, `knobs_set`,
`param_expression_set/get`, `param_keyframe_set/remove`,
`param_keyframes_get`, `param_animated_get`, `plugins_search`, `layers_list`,
`exec_python` (AST-gated, enabled by default), `render_start`,
`render_status`, `tasks_list`, `events_poll`, plus M1's `sessions_spawn`,
`session_kill`, `session_restart`, `session_modal_status`,
`session_modal_dismiss`.

All tool docstrings are the agent-facing contracts. Op naming on the wire
mirrors the Python API 1:1 (`project.*`, `nodes.*`, `knobs.*`, `params.*`,
`render.*`, `plugins.*`, `exec.*`, `session.*`, `meta.*`).

## Safety

- `exec_python` is enabled by default but AST-gated (deletion, subprocess,
  eval/exec, write-mode open, app.closeProject/resetProject, dynamic
  indirection, homoglyphs are blocked; network calls require `timeout=`).
  Server-side and bridge-side checks.
- Every op is appended to `~/.Natron/natron-mcp/audit-<session>.jsonl`.
- `NATRON_MCP_DISABLE_EXEC=1` turns exec off; `NATRON_MCP_READONLY=1` starts
  a read-only session.

## Platform notes

- Windows: fully verified (portable build; GUI + headless contract tests).
- Linux: verified against the fork's Linux AppImage under WSL2/Xvfb —
  35/35; see `docs/LINUX-VERIFICATION.md` for required distro packages and
  caveats (OS-level modal dismissal is Windows-only and degrades gracefully).
- No Node.js runtime; no OS-specific code paths in bridge or server beyond
  the explicitly-fenced process-control and modal layers.

## Architecture notes (learned in M0/M1, see docs/)

- Natron's Python API is main-thread-only; the bridge queues every op to the
  main thread and never blocks it for long (renders queue and are polled).
- Headless Natron has a QCoreApplication but **no running event loop** — the
  bridge pumps `processEvents()` so deferred work (e.g. node deletion)
  completes.
- `App` inherits the `Group` wrapper: `app.getChildren()` enumerates nodes.
- Modal dialogs opened by *Python-initiated* calls hold the GIL across the
  C++ call: in-host watchdogs cannot fire. The server provides OS-level
  detection/dismissal (Win32 via ctypes) plus session lifecycle ops —
  see `docs/M1-NOTES.md`.

## License

GPL-2.0 — see [`LICENSE`](LICENSE). The bridge loads inside Natron (GPLv2);
keeping the whole project GPLv2 matches the host's license.
