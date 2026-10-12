# Natron MCP Plugin — Implementation Plan (rev 2)

Status: **implemented.** M0 complete 2026-10-10; M1 (host lifecycle + modal
handling) 2026-10-11; Linux/AppImage verification 2026-10-12 — see
`M0-VERIFICATION.md`, `M1-NOTES.md`, `LINUX-VERIFICATION.md` in this folder.
This document is the original design rationale; the §15 open items were
settled 2026-10-10 and implementation started in `tools/mcp/` of the fork
(since extracted into this standalone repo).
Rev 2 changes: tools re-verified against Natron's own API (Nuke-only concepts
removed), cross-platform priority applied to every decision, lean-core-first
tool policy, all ~90 suggestions kept as commented backlog, user decisions
from review incorporated.

Targets this workspace: Niik-l Natron 2.6 fork (Qt6/PySide6, Python 3.14,
Shiboken6, Cycles embedded, 3D system, deep EXR, OpenVDB/FastVolumeRender).

---

## 0. Decisions (from plan review)

| # | Decision |
|---|---|
| D1 | **Modes: GUI + headless.** The bridge runs inside `Natron` (GUI), `NatronRenderer`, and `natron-python` sessions; the MCP server multiplexes several host instances. |
| D2 | **Transports: stdio + streamable-HTTP.** stdio for local clients; HTTP behind a flag, bound to `127.0.0.1` by default, token auth. |
| D3 | **Tool policy: lean core first.** Phase 1 implements ~30 core tools only. All ~90 suggestions preserved as *commented backlog* (§7). |
| D4 | **`exec_python` enabled by default** (still AST-gated and audit-logged; disable with `NATRON_MCP_DISABLE_EXEC=1`). |
| D5 | **Lives inside this fork**: `Natron/tools/mcp/` (bridge, server, tests, docs). Optional staging into `Natron-install` by the win-build pipeline. |
| D6 | **Pixel returns: hybrid** — preview-style ops return an inline (base64, size-capped) image *plus* the full-resolution file path; data-style ops return file paths by default with inline opt-in (explanation in §10). |
| D7 | **Cross-platform priority** (review directive) — see §3. No Node.js runtime, no OS-specific code paths. |

Remaining defaults (unless overridden): server packaged via pip/venv (pure
Python); autoload opt-in via `~/.Natron/initGui.py`; any MCP client supported
(no client-specific features); per-op undo entries with optional op-group
batching.

## 1. What the three Nuke projects teach (reference study)

| Project | Architecture | Adopted | Rejected |
|---|---|---|---|
| dughogan/nuke_mcp | In-host TCP addon (9876) + external FastMCP server + client | 3-tier split, start/stop panel, escape-hatch exec | tiny tool set, no safety/task machinery |
| joshw051/nuke-mcp | Same split, hardened (request-id echo, heartbeat, keepalive, main-thread marshalling) | wire-protocol hardening, lazy profiles, MCP Tasks primitive, Pydantic outputs, AST scanner, read-only audits, prompts | Nuke-only domain macros (Karma AOVs, CopyCat, STMap, CameraTracker) |
| linchpinhq/nuke-mcp | Node.js server + paste-in bridge | domain-macro idea | Node.js dependency (violates D7), paste-in install fragility |

## 2. Natron API verification (grounding for every tool)

Verified in this workspace (`Natron/Engine/Py*.h`, `Gui/`, `Engine/Dev/`):

- **App** (`PyAppInstance.h`): `createNode(pluginID, props)`, `createReader`,
  `createWriter`, `render(writeNode, first, last, step)` (+list form),
  `getProjectParam`, `saveProject/loadProject/saveTempProject`,
  `addProjectLayer(ImageLayer)`, `getPluginIDs(filter)`,
  `writeToScriptEditor`, `addFormat`.
- **Node** (`PyNode.h`): `connectInput(i, node)`, `disconnectInput(i)`,
  `canConnectInput`, `destroy(autoReconnect)`, `setPosition(x, y)`,
  `getInput*`, script-name/label, user param creation
  (`createIntParam...createStringParam`), image-plane introspection
  (`ImageLayer`: RGBA/motion/disparity, layer components).
- **Params** (`PyParameter.h`): `setValue/getValue`, `setValueAtTime/
  getValueAtTime`, `getNumKeyframes`/keyframe-by-index access,
  `setInterpolationAtTime`, `removeKeyframe`, `setExpression/getExpression`,
  `getIsAnimated`, full knob metadata table (min/max/options/flags),
  dynamic user-knob creation.
- **Roto** (`PyRoto.h`): layers/items, `BezierCurve.addControlPoint/
  addControlPointOnSegment/removeControlPointByIndex/movePointByIndex/
  setPointAtIndex`, feather points (`getFeatherPointPosition`...), shape
  keyframes — full programmatic rotopaint.
- **Tracker** (`PyTracker.h`): `Track.createTrack/getAllTracks/
  getSelectedTracks/startTracking(tracks, range, forward)/stopTracking`,
  per-track params — 2D tracking only (no 3D camera solver in Natron).
- **Fork node families** (`Engine/Dev/`): `Cycles` (CyclesRender,
  CyclesRenderSettings, CyclesPassRender, CyclesRenderPassManager,
  KnobPassTable), `Deep`, `FastVolume`, `Particles`, `Scene3D`, `PostFX`,
  `Transform`, `Channel`, `Color`.
- **Real plugin IDs seen in this build's verify run**: `fr.inria.built-in.
  {DeepRead, DeepWrite, DeepMerge, Roto, RotoPaint, Tracker, Viewer, Write,
  Read, ReadAlembicArchive/Camera/Transform, Sphere3D, Scene3D, Camera3D,
  Light3D, CyclesRender, CyclesRenderPass, CyclesRenderSettings,
  fr.inria.openfx.WriteEXR, WriteFFmpeg}`, plus G'MIC (`eu.gmic.*`),
  community PyPlugs, Shadertoy GLSL, SeExpr.
- **M0-verify list** (exists in C++, Python-binding presence unconfirmed):
  viewer access from Python (`Gui::getActiveViewer/getViewersList` — needs
  binding check), bulk keyframe time listing (iterate `getNumKeyframes` +
  index getter), render progress callbacks (`onRenderStarted`/
  `onRenderFrame`? or knob-render-progress), `natron.getGuiInstance(i)`.
  Each M0 item gets a documented fallback if the binding is missing.

**Removed as Nuke-only** (was in rev 1, has no Natron equivalent):
CameraTracker/3D camera solve, CopyCat/ML training, Karma AOV pipeline,
STMap/IDistort/SmartVector bake, LiveGroups, Toolsets. Natron-native
replacements are listed in §7.

## 3. Cross-platform decisions (D7, applied everywhere)

- **No Node.js** (linchpinhq uses it) — the MCP server is pure Python
  (`fastmcp`), runs identically on Windows/macOS/Linux.
- **No OS-specific code** in bridge or server: sockets via `socket` (TCP
  localhost only by default), temp artifacts via `tempfile`, all paths in
  protocol payloads normalized to forward slashes, LF framing, no symlinks,
  no registry/COM/Applescript, atomic file writes via temp+rename.
- **Pixel analysis in the server**: `numpy` + `OpenEXR`/`imageio` pip wheels
  (cross-platform); `oiiotool` CLI used opportunistically if present on
  PATH (it ships with this MSYS2 build); never a hard dependency.
- **Headless parity**: the same ops must work in GUI and headless mode —
  viewer ops report "unavailable: headless" instead of failing silently.
- **Packaging**: `pip install -e Natron/tools/mcp/server` (venv/pipx);
  optional `--http` mode; no admin rights required anywhere.
- **Build pipeline**: win-build `06-install.sh` may stage `tools/mcp/bridge`
  into `Resources/`; Linux build scripts get the same optional step (the
  fork already builds Linux AppImages via CI).

## 4. Architecture

```
┌─────────────┐ stdio+HTTP ┌──────────────────────┐ TCP JSON frames ┌──────────────────────────┐
│ MCP clients │◄──────────►│ natron-mcp server    │◄───────────────►│ bridge addon (in-host)   │
│ (any)       │            │ tools, profiles,     │ req-id, events, │ socket thread + Qt main- │
│             │            │ tasks, safety, audit │ heartbeat       │ thread dispatcher        │
└─────────────┘            └──────────┬───────────┘                 └────────────┬─────────────┘
                                      │ session multiplexing                     │ main thread only
                                      ▼                                          ▼
                          N host instances (GUI, NatronRenderer, natron-python)
                                      Natron Python API
```

- **Bridge addon** (`Natron/tools/mcp/bridge/natron_mcp_bridge/`): TCP server
  thread (127.0.0.1, port chosen at start, written to a well-known session
  file), op dispatcher, main-thread executor (PySide6 queued signal → slot →
  result promise), event emitter (progress/log), mode reporting
  (`gui:0` / `renderer:<pid>` / `python:<pid>`), autoload via
  `~/.Natron/initGui.py` (GUI) and `~/.Natron/init.py` or `natron-python`
  script arg (headless).
- **Server** (`Natron/tools/mcp/server/natron_mcp/`): FastMCP; tool modules
  per profile; Pydantic result models; Tasks state machine with disk
  persistence; AST-gate evaluator runs *server-side* (blocks before wire);
  audit log (JSONL); session registry + discovery file
  (`~/.Natron/natron-mcp/sessions.json`) so clients can `sessions_list`.
- **Protocol**: NDJSON frames, `{"id", "op", "args"}`, responses
  `{"id", "ok", "result"|"error"}`, events `{"event", "data"}`; heartbeat
  2 s; op timeouts + cancel tokens; protocol version handshake including
  Natron build id (`getNatronVersionString()` + build commit).

### Headless mode (D1)
- `sessions_spawn(mode="renderer"|"python", script=..., port=0)` → server
  starts a headless Natron process with the bridge preloaded, returns a
  session id; all ops addressable per session (`session` arg, default
  "primary").
- Use cases: agent-driven batch renders, QC sweeps, script conversion,
  farm-style pipelines. GUI and headless sessions are peers.

## 5. Main-thread dispatch

- Socket thread → op queue → PySide6 `QMetaObject.invokeMethod` (queued) on a
  dispatcher `QObject` → slot executes op → result posted back.
- GUI thread never blocks > ~50 ms: heavy ops return immediately as Tasks.
- Render ops use `app.render()` (non-blocking form) or a worker thread for
  blocking forms; progress comes from Natron callbacks as events (M0-verify).
- Headless `natron-python`: no Qt event loop — dispatcher runs a simple
  select-loop on the main interpreter thread instead.

## 6. Phase 1 — lean core tool set (~30 tools, the implemented surface)

1. `session_list`, `session_info` (mode, natron build, project path)
2. `project_info` (settings, frame range, format, views)
3. `project_set_settings` (range/format/fps/views)
4. `project_save` / `project_load` / `project_new`
5. `nodes_list` (id, name, type, plugin id, inputs, position, enabled)
6. `node_get` (all knobs incl. non-default values + metadata)
7. `node_create` (`createNode`/`createReader`/`createWriter` + props)
8. `node_delete` (`destroy`)
9. `node_connect` / `node_disconnect` (`connectInput`/`disconnectInput`)
10. `node_position_set` (`setPosition`)
11. `knobs_get` (node, filter)
12. `knobs_set` (batch set; supports per-key value/time for animation)
13. `param_expression_set` / `param_expression_get` (`setExpression`)
14. `param_keyframe_set` (`setValueAtTime`), `param_keyframe_remove`,
    `param_animated_get` (`getIsAnimated`), `param_interpolation_set`
15. `plugin_search` (query `getPluginIDs(filter)` + knob-name introspection
    of a scratch instance — how agents discover 2,348 plugins safely)
16. `group_create` / `group_list_contents`
17. `exec_python` (AST-gated, default-enabled per D4; captures stdout/stderr
    and return value)
18. `undo` / `redo` (per-op undo entries, D-default)
19. `render_start` (Task), `render_status`, `render_cancel`
20. `layers_list` (`ImageLayer` planes incl. motion/disparity),
    `layer_add` (`addProjectLayer`)
21. `events_subscribe` (MCP resource/subscription for progress+log stream)
22. `docs_tool` (returns the tool-contract doc for one op — self-describing
    surface for agents)

Non-goals for Phase 1: viewer ops, pixels, deep/VDB, roto/tracker details,
cycles-specific macros (all in backlog §7).

## 7. Full backlog — all ~90 suggestions (kept per D3 as comments)

Not implemented in Phase 1; sequenced M2–M4. Every entry is Natron-verified
(§2) or marked *M0-verify*.

### render / tasks
```python
# render_frames(write_node, first, last, step, views) -> Task       # app.render(list-form)
# render_settings_get/set(write_node)                                # Write node knobs
# render_region(node, roi, time) -> temp EXR path                    # ROI render
# render_batch(write_nodes[], ranges) -> Task                        # list-form render
# render_progress_events(task_id)                                    # events: per-frame
```
### viewer (GUI sessions only; M0-verify bindings)
```python
# viewer_list / viewer_connect_input / viewer_set_view_time
# viewer_screenshot(node?, -> PNG temp path)                         # viewer buffer
# viewer_playback_set(first, last, current)
# viewer_channels_set(layer)
```
### image-data (pixels; analysis server-side, D6)
```python
# pixels_read(node, time, view, layer, roi, format=exr|png|npy) -> path|inline
# pixels_stats(node, layer, roi) -> per-channel min/max/mean/histogram
# histogram_image(node, layer, roi) -> PNG (inline, capped)          # vision-friendly
# pixels_compare(nodeA, nodeB, roi) -> diff stats + diff PNG
# scanline_preview(node, roi, max_px=512) -> inline PNG thumbnail
```
### color
```python
# ocio_configs_list / ocio_working_space_get/set
# node_colorspace_get/set                                            # Read/Write/ColorSpace knobs
# audit_colorspaces() -> report                                      # read-only
```
### cycles (fork-specific; knobs of Engine/Dev/Cycles nodes)
```python
# cycles_settings_get/set(node)                                      # samples, device, denoise
# cycles_passes_list/set(node)                                       # CyclesPassRender + KnobPassTable
# cycles_render(node, range) -> Task (sample/row progress events)
# cycles_gpu_devices_list
# scene3d_dump(node)                                                 # objects/lights/cameras in Scene3D chain
# scene3d_setup_preset(kind)                                         # camera+light+sphere rig macro
```
### deep (built-ins verified present)
```python
# deep_nodes_list                                                    # DeepRead/Write/Merge/... enumeration
# deep_stats(node, time, roi) -> sample counts/coverage
# deep_read_tile(node, time, roi) -> JSON samples / temp EXR
# deep_holdout_chain(fg, bg) -> macro graph
```
### vdb / fastvolume (fork-specific)
```python
# vdb_list_grids(file) -> names, bboxes, voxel types                 # server-side pyopenvdb? M0-verify; else OIIO/vdb tool
# vdb_slice_png(file, grid, axis, offset) -> image
# fastvolume_settings_get/set(node)
```
### roto (PyRoto, verified)
```python
# roto_items_list(node)                                              # layers + bezier items
# roto_shape_create(node, layer, points[], feather)                  # addControlPoint/setPointAtIndex
# roto_shape_get(node, shape, time) -> control+feather points
# roto_shape_animate(node, shape, keys[])                            # movePointByIndex over time
```
### tracker (PyTracker, verified — 2D only)
```python
# tracker_tracks_list(node)                                          # getAllTracks
# tracker_track_create(node)                                         # createTrack
# tracker_run(node, tracks, range, forward) -> Task                  # startTracking/stopTracking
# tracker_data_get(node, track, range) -> per-frame params
```
### io / assets
```python
# read_scan(path_pattern) -> sequence info, missing frames           # server-side glob
# readers_relink(old_root, new_root)                                 # batch path fix (macro)
# alembic_inspect(file) -> object hierarchy
# script_xml_get(nodes=[...]) -> raw project XML subtree             # zero-loss dump
# script_xml_diff(before, after) -> structural diff                  # agent feedback loop
# nodes_export_pyplug(nodes, name) / nodes_import_pyplug(path)       # Natron-native "toolset"
```
### audit (read-only)
```python
# audit_project() -> unconnected/disabled/missing-file/cache issues
# audit_write_paths() -> absolute-path drift, overwrites
# audit_render_settings() -> format/fps/range mismatches
```
### prompts (first-class MCP prompts, M3)
```python
# prompt_build_keying_chain / prompt_cycles_render_setup
# prompt_deep_holdout / prompt_vdb_volume_render
# prompt_sequence_qc_sweep / prompt_match_grade_ab
```

## 8. Safety model

- AST gate on `exec_python` (server-side, before wire): deny `os.remove/
  unlink/rmtree`, `shutil.rmtree`, `subprocess.*`, write-mode `open()`
  (AST-detected), `eval/exec/compile/getattr/__import__`, `globals/vars/
  sys.modules`, import-alias indirection, unicode homoglyphs; allowlist
  Natron modules; blocklist `app.closeProject/resetProject` bare calls
  (must go through `project_*` ops). Enabled by default (D4); disable via
  `NATRON_MCP_DISABLE_EXEC=1`.
- Session permission levels (`readonly` | `normal`) selectable in the bridge
  panel; `readonly` denies all mutating ops (exec included).
- Audit log JSONL per op: ts, session, op, args digest, result status,
  duration, caller (client id). Append-only.
- Mutating ops wrapped in undo; `safe=True` (default) snapshots the project
  to temp before batch mutations.

## 9. MCP Tasks primitive (renders, tracking, batches)

State machine `working / input_required / completed / failed / cancelled`
persisted at `~/.Natron/natron-mcp/tasks/<id>.json`; progress events over the
wire; graceful cancel at frame/track boundaries; resume after client or host
restart. Applies to: `render_*`, `tracker_run`, `render_batch`, future
heavy macros.

## 10. Pixel returns: file paths vs inline (D6 explanation)

Two return modes for pixel-producing ops (`pixels_read`,
`viewer_screenshot`, `histogram_image`, `vdb_slice_png`, ...):

- **File-path mode (default)**: the bridge/server writes the artifact to a
  temp dir (EXR/PNG/NPY) and returns `{path, format, stats_summary}`.
  - Pros: no size limits, full float precision (EXR), fast for big ROIs,
    cacheable by path hash, the agent's own tools (code execution) can
    post-process at full quality.
  - Cons: the *model* itself cannot "see" the file — an agent without
    code-execution or file access cannot interpret it directly.
- **Inline mode (opt-in per call, size-capped)**: returns the image encoded
  as an MCP image content block (base64 PNG/JPEG), capped (default ≤ 1.5 MB,
  auto-downscaled to ≤ 1024 px longest side unless `full_res=True`).
  - Pros: vision-capable clients (Claude, etc.) see the image *in the
    conversation* — one round-trip for "what does this look like".
  - Cons: token cost, no float precision, latency for large data.
- **Hybrid default for look-checks**: `scanline_preview` returns an inline
  thumbnail *and* the full-res EXR path in one response.

Decision rule encoded in the server: `return="path" | "inline" | "both"`,
with `inline` refused above the cap unless explicitly forced.

## 11. Packaging (inside the fork, D5)

```
Natron/tools/mcp/
  bridge/natron_mcp_bridge/   # pure Python, no 3rd-party deps beyond PySide6
  server/natron_mcp/          # fastmcp + pydantic + numpy/OpenEXR extras
  tests/                      # unit + contract tests
  docs/                       # tool contracts, client configs (stdio+http)
  pyproject.toml              # server installable: pip install -e tools/mcp/server
```
- win-build `06-install.sh` optionally stages `bridge` into
  `Natron-install/Resources/natron-mcp/` and writes the `initGui.py` snippet
  (opt-in, commented by default). Linux AppImage CI gets the same optional
  step.
- Client config samples for Claude Desktop / Claude Code / Cursor included.

## 12. Testing

- Unit: protocol codec, AST gate (incl. homoglyph fuzz), task state machine,
  path normalization.
- Contract (cross-platform, headless): spin real `NatronRenderer`/
  `natron-python` processes (`NATRON_BIN` env), run op sequences, assert
  project-XML golden diffs and rendered EXRs. GUI-only ops skipped in CI.
- Replay corpus: every tool with before/after XML snapshots.

## 13. Milestones

| M | Scope | Exit criteria |
|---|---|---|
| M0 **DONE** | Bridge skeleton (GUI + one headless session), dispatch, wire protocol; **M0-verify list resolved** | 25/25 tests pass (12 unit / 9 bridge contract vs live headless NatronRenderer / 4 MCP-layer FastMCP end-to-end); findings in `Natron/tools/mcp/docs/M0-VERIFICATION.md`; delivered 2026-10-10 in `Natron/tools/mcp/` |
| M1 | Lean core (§6) complete + audit log + undo + contract tests | Golden-XML suite green; exec_python gated & logged |
| M2 | Backlog wave 1: image-data, color, render/tasks | Agent renders and inspects pixels end-to-end |
| M3 | Backlog wave 2: cycles/deep/vdb, roto/tracker, prompts; Tasks resume | Long Cycles render survives client restart |
| M4 | Session multiplexing hardening; packaging into build pipeline; docs/demo | Farm-style headless demo on Windows + Linux CI |

## 14. Risks

- Python-binding gaps (viewer, bulk keyframes, render callbacks) — M0 gates
  them; every gap has a documented fallback (temp-Write renders, iteration
  over keyframe index API).
- Main-thread stalls — enforced by dispatch policy + Task-ification.
- winpthreads quirks (as met in the build) — bridge avoids `std::` threading
  entirely (pure Python + Qt signals).
- Protocol drift between bridge/server — versioned handshake + contract
  tests.
- 2,348-plugin discovery — `plugin_search` + lazy knob introspection, never
  full enumeration in context.

## 15. Open items before implementation

All settled 2026-10-10: Phase-1 core list confirmed as implemented (§6 now
26 tools); HTTP token auth deferred to M3 hardening; install staging and
autoload remain opt-in; docs in English. Implementation started — see
`Natron/tools/mcp/` (M0 complete, details in `tools/mcp/docs/M0-VERIFICATION.md`).
