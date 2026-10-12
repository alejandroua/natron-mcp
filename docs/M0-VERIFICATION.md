# M0 verification results (live probes against real headless Natron 2.6 fork)

> 2026-10-11: host lifecycle + modal-dialog handling added on top of M0 —
> see `docs/M1-NOTES.md` (spawn/kill/restart, OS-level modal tools, server
> watchdog, exec_python network-timeout gate).

All findings verified by driving `NatronRenderer.exe` (this build) through the
bridge during M0. Contract tests in `tests/test_contract_headless.py` encode
these behaviors.

## Verified working (Python API, Python 3.14 / shiboken6)

| API | Notes |
|---|---|
| `app.createNode(plugin_id)` | `net.sf.openfx.NoOpPlugin`, `fr.inria.built-in.Dot/BackDrop` verified. `fr.inria.built-in.NoOp` does NOT exist (Nuke name). |
| `app.createReader/createWriter(file)` | auto-picks the reader/writer plugin; works with `####.png` / `.exr` sequence patterns |
| `app.getChildren()` | enumerates root nodes — `App` inherits the `Group` wrapper; `getNodes` does not exist |
| `app.getNode(scriptName)` | returns the node; the wrapper stays callable after `destroy()` until GC — do not rely on exceptions for deletion checks, use `getChildren()` |
| `node.createDoubleParam(name, label)` + `param.setValue/getValue/setValueAtTime/getValueAtTime/getIsAnimated` | user params work headless, no `refreshAndPersistParams` needed (that method does not exist in this fork) |
| `param.setExpression(expr, has_ret)` | `has_ret=True` requires the expression to define `ret` (`ret = 3.5`); plain value expressions must use `has_ret=False`. The bridge auto-detects (`ret` + `=` in the expression) |
| `param.getExpression(dim)` | returns `(expr, has_ret)` style pair in our adapter |
| keyframes bulk API | `getNumKeys(dim)`, `getKeyIndex`, `getKeyTime(index, dim)` (NOT `getNumKeyframes`/`getKeyframeTime` — those names do not exist); `deleteValueAtTime`, `removeAnimation`, `getDerivativeAtTime`, `getIntegrateFromTimeToTime`, `getCurrentTime` exist |
| `Param.getTypeName()` | returns e.g. `Bool`, `Choice`, `Double`, `Int`, `String`, `Page`, `Button`, `Separator` — used for value dispatch |
| `getValue(dim)`/`setValue(v, dim)` | single-dimension params take NO dimension argument (`TypeError`); adapter falls back automatically. `Page`/`Button`/`Separator` params have no getValue |
| `app.getPluginIDs(filter)` | on `NatronEngine.natron` (the `PyCoreApplication` singleton), NOT on `App`. 1,976 plugins registered headless (2,348 in GUI) |
| `NatronEngine.natron.getNatronVersionString()` | also on the core singleton, not on App |
| `app.saveProject(path)` | works headless; writes .ntp XML |
| `app.render(write_node, first, last, step)` | NON-blocking (queues writers); completion tracked by output-file checks in M0 |
| `node.connectInput(i, node)` / `disconnectInput` / `setPosition` | verified |
| `node.destroy(autoReconnect)` | returns None; deletion is DEFERRED (see event loop note) |

## Key behaviors

1. **Headless event loop**: NatronRenderer has a `QCoreApplication` but no
   running event loop. Queued internal work (node deletion!) only completes
   when someone pumps `processEvents()`. The bridge does this after every
   request and on accept timeouts. Verified: without pumping, a deleted node
   stays in `getChildren()` indefinitely.
2. **Script execution**: Natron exec's bootstrap scripts without `__file__`
   and rejects unknown CLI args — the bootstrap locates the bridge package via
   `NATRON_MCP_BRIDGE_DIR` env (fallback: `sys.argv[0]`), and all runtime
   config is passed via env (`NATRON_MCP_PORT`, `NATRON_MCP_READONLY`).
3. **`app` global** is available in scripts run by NatronRenderer; the bridge
   binds it explicitly via `bind_app(app)` for reliability.
4. **GUI-vs-headless detection**: `import NatronGui` fails in NatronRenderer
   (headless builds do not bundle the Gui module). `mode=gui|renderer|headless`.
5. **Plugin counts** differ: headless 1,976 vs GUI 2,348 (community PyPlugs and
   GLSL nodes register only in GUI mode).

## Gaps found (with fallbacks in place / deferred)

| Gap | Impact | Fallback / plan |
|---|---|---|
| No per-frame render callbacks (`onRenderStarted` etc. not project params) | no native progress events | M0: output-file existence polling in `render.status`; M2: check `AppSettings` params + Writer progress knobs |
| Viewer APIs not on `App` (and unavailable headless anyway) | no viewer ops in M0 | M2 with a GUI session: probe `natron.getGuiInstance(0)` viewer wrappers |
| No `refreshAndPersistParams` | user-param registration | not needed headless (params persist via knobs.set/getValue); revisit for GUI PyPlug parity |
| Undo API not exposed to Python | no `undo` tool in Phase 1 | M1: pre-op project snapshot (temp .ntp) + `project.load` restore |
| `DoubleNodeCreationProperty` not importable | label-at-create limited | set knobs after creation instead (works) |

## GUI-session findings (live Natron.exe, tested 2026-10-10)

7 GUI contract tests run against a real GUI session (window on the desktop):
`test_contract_gui.py`. The GUI bridge works, but two crash classes were
found and addressed:

1. **FIXED — Qt calls from the serve thread.** `QTimer`/`QMetaObject.invokeMethod`
   called from our socket thread (a foreign, non-QThread thread) recursed
   inside Qt6Core and segfaulted (gdb: one frame repeated ~60×, thread
   "natron-mcp-bridge", exit code 0xC0000409 fail-fast). Fix: the serve thread
   does **zero Qt work**; a persistent `QTimer` created on the *main thread*
   (in `start_gui`) drains the op queue every 100 ms.
2. **FIXED 2026-10-11 — serve thread still raced the main-thread dispatcher
   (root cause of the Cherry Studio crash, 2026-10-11 11:33).** Although the
   QTimer fix above moved op draining to the main thread, `start_gui` left
   `BridgeServer.mode` at its `"auto"` default, so `_serve_loop`'s
   `if self.mode != "gui"` guard never skipped draining in GUI mode. The
   serve thread therefore *also* ran `_drain_op_queue()` (executing ops on a
   foreign thread, racing the QTimer — an op could run twice or on the wrong
   thread) and `_pump_events()` → `QCoreApplication.processEvents()` from a
   non-Qt thread. Observed live: creating `Sphere3D` succeeded (drained by the
   main thread) and creating `Light3D` immediately killed the process with
   SIGSEGV inside Qt6Core on the "natron-mcp-bridge" thread (gdb, deterministic
   offset Qt6Core+0x9042, called from Qt6Core+0x33ccae ← +0x33ccc6, corrupt
   unwind; headless NatronRenderer creates all of Sphere3D/Scene3D/Light3D/
   Material3D/CyclesRender/CyclesRenderSettings in ~0.1–0.2 s). This also
   explains the earlier "deferred node destroy" crashes below — deletes raced
   onto the serve thread the same way. Fix in `bridge/server.py`:
   `start_gui` sets `srv.mode = "gui"` *and* `_serve_loop` now decides
   headless draining by `self._qtdisp is None` (dispatcher presence) instead
   of the mode string, so the serve thread can never touch the op queue or Qt
   in GUI sessions regardless of mode detection. Verified: 32/32 tests pass,
   all 3D-node creates succeed in GUI, and a full scene
   (Sphere3D×5 + Light3D×3 + Scene3D + Camera3D + CyclesRender +
   CyclesRenderSettings + Viewer + Write) builds, wires and renders frame 1
   to EXR through the FastMCP layer.

3. ~~**KNOWN FORK BUG (M1 fix target) — deferred node destroy.**~~ Reattributed
   to bug #2 above (delete ops raced onto the serve thread). The
   `auto_reconnect=False` GUI default and last-test-ordering mitigations stay
   as defense in depth; re-evaluate at M1 whether the C++ destroy path still
   needs work.

Operational notes for GUI automation:
- Natron must run with its normal install layout as cwd (or plugins are fewer);
- never pipe the GUI's stdout through a pipe that nobody drains — it blocks
  startup; redirect to a file;
- plugin count: ~1,978 in our test sessions in BOTH modes; the release's
  build-time verify reported 2,348 — parity to investigate in M2 (suspect
  PyPlug registration differences under the harness environment);
- a fresh session needs ~15–25 s before it is safe to mutate the graph.

## Test results (M0 exit)

- 12 unit tests (protocol codec, AST gate incl. homoglyph/indirection cases)
- 9 bridge contract tests vs live headless NatronRenderer
- 4 MCP-layer tests (FastMCP tool surface → wire → bridge → Natron)
- 7 GUI contract tests vs live Natron.exe (window automation)
- **32/32 passing in one combined run**
