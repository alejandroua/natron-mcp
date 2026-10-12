# M1 notes — host lifecycle + modal dialogs (2026-10-11)

Covers the two operational gaps hit in live Cherry Studio use:
modal dialogs wedging the GUI, and no way to start/stop/restart hosts.

## Why modals wedge the bridge (measured, not assumed)

When a **Python-initiated** modal dialog opens in the host GUI (Natron's
"Save changes?" on `resetProject`, a script-triggered file dialog, ...), the
main thread enters the dialog's nested event loop **while holding the GIL
across the whole C++ call**. Measured consequences (GDB + debug trace):

- The Qt event loop keeps pumping (window responds to the OS), but **no
  Python code executes anywhere in the host**: the bridge's QTimer drain
  produces zero ticks, so every op — including `session.modal_status` —
  times out, and an in-host watchdog cannot fire either.
- A queued op blocked on the dialog never returns; further ops queue behind it.

Therefore modal handling cannot live only inside the host. The MCP server
process now provides **OS-level** detection and dismissal
(`server/natron_mcp/modal_windows.py`, Win32 via ctypes): enumerate the host
PID's visible top-level windows, identify dialog candidates, dismiss via
`WM_CLOSE` (acts as cancel/reject on QDialogs) or `BM_CLICK` on child buttons
when native button HWNDs exist (Qt often renders buttons without them).

Verified live: forced `app.saveProject('')` wedged the bridge; OS scan saw
"Save File" (914x438); `WM_CLOSE` dismissed it; bridge answered `hello` and
`meta.instance_info` again immediately after.

Two wedge classes, two layers:

| Wedge class | GIL | In-host watchdog | OS-level tools |
|---|---|---|---|
| C++-initiated modal (autosave-restore prompt, error dialogs) | free | works (auto-answers) | fallback |
| Python-initiated modal (save-changes on reset, save dialogs) | held | unreachable | **required** |

## What was added

Bridge (in-host):
- `project.new` pre-saves the (possibly dirty) project to a scratch `.ntp`
  before `resetProject`, so the save-changes modal never appears from
  automation. Side effect: the project filename association moves to the
  scratch file.
- `QtDispatcher` modal watchdog + re-entrancy guard: while an op is in flight
  and a modal widget is active, auto-answer after a 1.5 s grace
  (`NATRON_MCP_MODAL_POLICY=discard|save|off`, default `discard`). Covers
  C++-initiated modals (GIL free). Ops may execute re-entrantly only inside a
  modal loop (graph is idle while a dialog waits for input).
- `session.modal_status` / `session.modal_dismiss` ops.
- `exec_python` gate: network calls (`urlopen`, `requests.*`, `urlretrieve`)
  require an explicit `timeout=` — an unbounded fetch from a script was the
  second wedge in the incident. `urlretrieve` is denied (no timeout support).

MCP server (out-of-host):
- `sessions_spawn(mode="gui"|"renderer")` — starts a Natron host with the
  bridge, waits for registration, waits for the plugin count to stabilize;
  auto-dismisses startup dialogs (e.g. the autosave-restore prompt after a
  prior crash) per policy.
- `session_kill(session_id)` — terminate the host process (PID from the
  session registry, which the bridge now reports).
- `session_restart(session_id, mode="")` — kill + spawn in one step.
- `session_modal_status` / `session_modal_dismiss(action, title_sub="")` —
  bridge-first with 3 s timeout, then OS-level fallback (works during a full
  wedge; reports `source: "os_scan"`).
- **Watchdog thread** (started with the server): every 3 s, per session, ping
  with a 1.5 s timeout; after two consecutive failures (~6 s), if the host
  process shows a dialog window, auto-answer per
  `NATRON_MCP_MODAL_POLICY` (default `discard`). Log:
  `~/.Natron/natron-mcp/watchdog.log`. It never acts on a responsive host.

## Agent playbook for wedges

1. `session_modal_status` — see what dialog is up (works during a wedge).
2. `session_modal_dismiss(action="discard")` — clear it (or "save"/"cancel").
3. If the host is truly stuck (no dialog, no response): `session_restart`.
4. Prevention: prefer `project_new` (auto pre-save) over manual
   `resetProject`; never run unbounded network I/O in `exec_python`.
