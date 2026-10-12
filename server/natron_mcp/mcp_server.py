"""FastMCP tool surface for Natron (Phase-1 lean core).

Every tool forwards to the in-host bridge over the wire protocol. The bridge
executes on Natron's main thread; heavy work is queued and polled.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from typing import Any

from .bridge_client import BridgeClient, BridgeError

_clients: dict[str, BridgeClient] = {}
_clients_lock = threading.Lock()


def _get_client(session: str | None = None) -> BridgeClient:
    key = session or ""
    with _clients_lock:
        c = _clients.get(key)
        if c is not None and c._sock is not None:
            return c
        if session:
            sessions = BridgeClient.discover_sessions()
            match = [s for s in sessions if s.get("session_id") == session]
            if not match:
                raise BridgeError(f"session not found or not reachable: {session}")
            c = BridgeClient(host=match[0]["host"], port=int(match[0]["port"]))
        else:
            c = BridgeClient.connect_auto()
        c.connect()
        _clients[key] = c
        return c


def _call(op: str, args: dict[str, Any] | None = None, session: str | None = None) -> Any:
    return _get_client(session).request(op, args)


def _call_timeout(op: str, args: dict[str, Any] | None, session: str | None,
                  timeout_s: float) -> Any:
    return _get_client(session).request(op, args, timeout_s=timeout_s)


def _session_pid(session: str | None) -> int | None:
    """PID of the target session from the registry (works even when the bridge is wedged)."""
    from . import config
    sid = session
    if not sid:
        try:
            sid = _get_client(None).hello and _get_client(None).hello.get("session_id")
        except Exception:
            sid = None
    sessions = config.discover_sessions()
    if sid:
        for s in sessions:
            if s.get("session_id") == sid:
                return s.get("pid")
    # unresponsive bridge: fall back to the raw registry (includes dead sockets)
    try:
        import json
        data = json.loads(config.SESSIONS_FILE.read_text(encoding="utf-8"))
        for s in data.get("sessions", []):
            if sid and s.get("session_id") == sid:
                return s.get("pid")
    except Exception:
        pass
    return None


def _watchdog_log(msg: str) -> None:
    from . import config
    try:
        with open(config.SESSIONS_DIR / "watchdog.log", "a", encoding="utf-8") as fh:
            fh.write("%.2f %s\n" % (time.time(), msg))
    except OSError:
        pass


_watchdog_started = False
_watchdog_lock = threading.Lock()


def start_modal_watchdog() -> None:
    """Background thread: detect wedged hosts (bridge unresponsive + a modal
    dialog window in the host process) and auto-answer per policy.

    This runs OUTSIDE the host process, so it works even when no Python code
    can execute inside the host (Python-initiated modal dialogs hold the
    host's GIL across the dialog exec — see tools/mcp/docs/M1-NOTES.md).
    """
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    import time as _time

    from .host_process import modal_policy
    from .modal_windows import dialog_candidates, modal_dismiss

    failures: dict[str, int] = {}

    def _loop() -> None:
        while True:
            _time.sleep(3.0)
            policy = modal_policy()
            if policy == "off":
                failures.clear()
                continue
            try:
                sessions = BridgeClient.discover_sessions()
            except Exception:
                sessions = []
            alive_ids = set()
            for s in sessions:
                sid = s.get("session_id")
                alive_ids.add(sid)
                pid = s.get("pid")
                ok = False
                if pid:
                    try:
                        c = BridgeClient(host=s["host"], port=int(s["port"]))
                        c.connect()
                        c.request("meta.instance_info", timeout_s=1.5)
                        c.close()
                        ok = True
                    except Exception:
                        ok = False
                if ok:
                    failures.pop(sid, None)
                    continue
                if not pid:
                    continue
                n = failures.get(sid, 0) + 1
                failures[sid] = n
                if n < 2:
                    continue  # require two consecutive failures (~6 s) before acting
                try:
                    cands = dialog_candidates(int(pid))
                except Exception:
                    cands = []
                if not cands:
                    continue
                _watchdog_log("session %s (pid %s) unresponsive x%d; dialogs: %s; auto-%s"
                              % (sid, pid, n, [w["title"] for w in cands], policy))
                try:
                    res = modal_dismiss(int(pid), policy)
                    _watchdog_log("dismiss result: %s" % res)
                except Exception as exc:
                    _watchdog_log("dismiss error: %r" % exc)
                failures[sid] = 0
            for sid in list(failures):
                if sid not in alive_ids:
                    failures.pop(sid, None)

    threading.Thread(target=_loop, name="natron-mcp-watchdog", daemon=True).start()


def _session_id(session: str | None) -> str | None:
    return session


async def serve_stdio() -> None:
    from fastmcp import FastMCP

    mcp = FastMCP("natron")
    start_modal_watchdog()

    @mcp.tool
    def sessions_list() -> dict:
        """List reachable Natron bridge sessions (GUI / NatronRenderer / natron-python)."""
        sessions = BridgeClient.discover_sessions()
        return {"sessions": [
            {k: s.get(k) for k in ("session_id", "mode", "host", "port", "readonly", "pid")}
            for s in sessions
        ]}

    @mcp.tool
    def sessions_spawn(mode: str = "gui", timeout: float = 120.0) -> dict:
        """Start a fresh Natron instance with the MCP bridge loaded and wait for it to register.

        mode: "gui" (full window) | "renderer" (headless NatronRenderer).
        Returns the new session (id, pid, port).
        """
        from .host_process import HostProcessError, spawn_host
        try:
            return spawn_host(mode, timeout)
        except HostProcessError as exc:
            return {"spawned": False, "error": str(exc)}

    @mcp.tool
    def session_kill(session_id: str, timeout: float = 10.0) -> dict:
        """Terminate the Natron host process behind a session id (wedged GUI recovery)."""
        from .host_process import HostProcessError, kill_session
        try:
            return kill_session(session_id, timeout)
        except HostProcessError as exc:
            return {"killed": False, "error": str(exc)}

    @mcp.tool
    def session_modal_status(session: str | None = None) -> dict:
        """Report whether a modal dialog is currently blocking the host GUI (titles, classes, buttons).

        Works even when the host is fully wedged: queried via the bridge first,
        with an OS-level window scan (host process windows) as fallback.
        """
        try:
            return _call_timeout("session.modal_status", None, session, 3.0)
        except Exception:
            pid = _session_pid(session)
            if pid:
                from .modal_windows import modal_status as os_modal_status
                st = os_modal_status(int(pid))
                st["source"] = "os_scan"
                st["note"] = "bridge unresponsive; answered from a window scan of the host process"
                return st
            return {"modal_active": None, "error": "no session/pid available"}

    @mcp.tool
    def session_modal_dismiss(action: str = "discard", session: str | None = None,
                              title_sub: str = "") -> dict:
        """Answer a modal dialog that is blocking the host GUI.

        action: "discard" | "save" | "cancel" | "close" — matched against dialog buttons
        (in-host), else the dialog window is closed (WM_CLOSE acts as cancel/reject).
        Works even when the host is fully wedged (OS-level fallback).
        """
        try:
            return _call_timeout("session.modal_dismiss", {"action": action}, session, 3.0)
        except Exception:
            pid = _session_pid(session)
            if not pid:
                return {"dismissed": False, "error": "no session/pid available for OS-level dismiss"}
            from .modal_windows import modal_dismiss as os_modal_dismiss
            res = os_modal_dismiss(int(pid), action, title_sub or None)
            res["source"] = "os_scan"
            return res

    @mcp.tool
    def session_restart(session_id: str = "", mode: str = "", timeout: float = 120.0) -> dict:
        """Kill a Natron host (if session_id given) and start a fresh one.

        mode: "gui" | "renderer" | "python"; empty reuses the killed session's mode.
        """
        from .host_process import HostProcessError, restart_session
        try:
            return restart_session(session_id, mode, timeout)
        except HostProcessError as exc:
            return {"restarted": False, "error": str(exc)}

    @mcp.tool
    def session_info(session: str | None = None) -> dict:
        """Info about the connected Natron instance: version, mode, project path, plugin count."""
        return _call("meta.instance_info", session=session)

    @mcp.tool
    def project_info(session: str | None = None) -> dict:
        """Project settings: frame range, format, fps, views, node count."""
        return _call("project.info", session=session)

    @mcp.tool
    def project_set_settings(settings: dict, session: str | None = None) -> dict:
        """Set project params, e.g. {"frameRange": [1001, 1100], "fps": 24}."""
        return _call("project.set_settings", {"settings": settings}, session=session)

    @mcp.tool
    def project_save(path: str, session: str | None = None) -> dict:
        """Save the current project to an .ntp XML file."""
        return _call("project.save", {"path": path}, session=session)

    @mcp.tool
    def project_load(path: str, session: str | None = None) -> dict:
        """Load a project file (replaces the current project)."""
        return _call("project.load", {"path": path}, session=session)

    @mcp.tool
    def project_new(session: str | None = None) -> dict:
        """Reset to a fresh empty project (destructive)."""
        return _call("project.new", session=session)

    @mcp.tool
    def nodes_list(session: str | None = None) -> dict:
        """List all nodes: script name, plugin id, label, inputs, position."""
        return _call("nodes.list", session=session)

    @mcp.tool
    def node_get(node: str, session: str | None = None) -> dict:
        """Full dump of one node: every knob with type, label, dimensions, current value, animation, expression."""
        return _call("nodes.get", {"node": node}, session=session)

    @mcp.tool
    def node_create(plugin_id: str, props: dict | None = None, session: str | None = None) -> dict:
        """Create a node by plugin id (use plugins_search to find ids). props: {"label": str}."""
        return _call("nodes.create", {"plugin_id": plugin_id, "props": props}, session=session)

    @mcp.tool
    def node_create_reader(filename: str, session: str | None = None) -> dict:
        """Create a Read node for a file/sequence (Natron picks the reader plugin)."""
        return _call("nodes.create", {"plugin_id": filename, "as_reader": True}, session=session)

    @mcp.tool
    def node_create_writer(filename: str, session: str | None = None) -> dict:
        """Create a Write node targeting a file/sequence."""
        return _call("nodes.create", {"plugin_id": filename, "as_writer": True}, session=session)

    @mcp.tool
    def node_delete(node: str, auto_reconnect: bool | None = None, session: str | None = None) -> dict:
        """Delete a node (optionally auto-reconnecting its neighbors). In GUI sessions the default is auto_reconnect=False: the fork's destroy(autoReconnect=True) path crashes the GUI graph (fork bug, see docs/M0-VERIFICATION.md)."""
        return _call("nodes.delete", {"node": node, "auto_reconnect": auto_reconnect}, session=session)

    @mcp.tool
    def node_connect(node: str, input_index: int, input_node: str, session: str | None = None) -> dict:
        """Connect input_node into node's input at input_index (0 = main input)."""
        return _call("nodes.connect",
                     {"node": node, "input_index": input_index, "input_node": input_node},
                     session=session)

    @mcp.tool
    def node_disconnect(node: str, input_index: int, session: str | None = None) -> dict:
        """Disconnect node's input at input_index."""
        return _call("nodes.disconnect", {"node": node, "input_index": input_index}, session=session)

    @mcp.tool
    def node_position_set(node: str, x: float, y: float, session: str | None = None) -> dict:
        """Set the node graph position of a node."""
        return _call("nodes.position_set", {"node": node, "x": x, "y": y}, session=session)

    @mcp.tool
    def knobs_get(node: str, filter: str | None = None, session: str | None = None) -> dict:
        """Get knobs of a node (optionally filtered by substring)."""
        return _call("knobs.get", {"node": node, "filter": filter}, session=session)

    @mcp.tool
    def knobs_set(node: str, values: dict, session: str | None = None) -> dict:
        """Set knob values in batch: {"size": 10, "label": "blur"}. Lists set multi-dimensional knobs."""
        return _call("knobs.set", {"node": node, "values": values}, session=session)

    @mcp.tool
    def param_expression_set(node: str, param: str, expression: str, has_ret: bool | None = None,
                             dimension: int = 0, session: str | None = None) -> dict:
        """Set a Python expression on a knob. For expressions using a 'ret' variable pass has_ret=True; plain value expressions auto-detect."""
        return _call("params.expression_set",
                     {"node": node, "param": param, "expression": expression,
                      "has_ret": has_ret, "dimension": dimension}, session=session)

    @mcp.tool
    def param_expression_get(node: str, param: str, dimension: int = 0,
                             session: str | None = None) -> dict:
        """Read the expression currently set on a knob."""
        return _call("params.expression_get",
                     {"node": node, "param": param, "dimension": dimension}, session=session)

    @mcp.tool
    def param_keyframe_set(node: str, param: str, time: float, value: float | int,
                           dimension: int = 0, session: str | None = None) -> dict:
        """Add or update a keyframe (setValueAtTime)."""
        return _call("params.keyframe_set",
                     {"node": node, "param": param, "time": time, "value": value,
                      "dimension": dimension}, session=session)

    @mcp.tool
    def param_keyframe_remove(node: str, param: str, time: float, dimension: int = 0,
                              session: str | None = None) -> dict:
        """Remove the keyframe at a given time."""
        return _call("params.keyframe_remove",
                     {"node": node, "param": param, "time": time, "dimension": dimension},
                     session=session)

    @mcp.tool
    def param_animated_get(node: str, param: str, dimension: int = 0,
                           session: str | None = None) -> dict:
        """Whether a knob is animated."""
        return _call("params.animated_get",
                     {"node": node, "param": param, "dimension": dimension}, session=session)

    @mcp.tool
    def param_keyframes_get(node: str, param: str, dimension: int = 0,
                            session: str | None = None) -> dict:
        """List keyframes of a knob (times and values via getNumKeys/getKeyTime)."""
        return _call("params.keyframes_get",
                     {"node": node, "param": param, "dimension": dimension}, session=session)

    @mcp.tool
    def plugins_search(filter: str = "", limit: int = 200, session: str | None = None) -> dict:
        """Search the thousands of registered node plugin ids by keyword."""
        return _call("plugins.search", {"filter": filter, "limit": limit}, session=session)

    @mcp.tool
    def layers_list(session: str | None = None) -> dict:
        """List views and image planes available in the project."""
        return _call("layers.list", session=session)

    @mcp.tool
    def exec_python(code: str, session: str | None = None) -> dict:
        """Execute Python in Natron (main thread). AST-gated: file deletion, subprocess, eval/exec, write-mode open, and app.closeProject are blocked. `app` global is available. Returns status, result, stdout, stderr."""
        return _call("exec.python", {"code": code}, session=session)

    @mcp.tool
    def render_start(write_node: str, first: int, last: int, step: int = 1,
                     session: str | None = None) -> dict:
        """Queue a render on a Write node (non-blocking). Poll render_status with the returned task_id."""
        return _call("render.start",
                     {"write_node": write_node, "first": first, "last": last, "step": step},
                     session=session)

    @mcp.tool
    def render_status(task_id: str, session: str | None = None) -> dict:
        """Status of a queued render task (state, outputs found)."""
        return _call("render.status", {"task_id": task_id}, session=session)

    @mcp.tool
    def tasks_list(session: str | None = None) -> dict:
        """List known tasks (renders etc.) in the session."""
        return _call("tasks.list", session=session)

    @mcp.tool
    def events_poll(limit: int = 20, session: str | None = None) -> dict:
        """Recent bridge events (hello, progress, log) for the connection."""
        client = _get_client(session)
        return {"events": client.recent_events(limit)}

    return mcp


def main_http(host: str = "127.0.0.1", port: int = 8765) -> None:
    import asyncio

    async def _run() -> None:
        mcp = await serve_stdio()
        print(f"natron-mcp: HTTP transport on http://{host}:{port}/mcp")
        await mcp.run_http_async(host=host, port=port)

    asyncio.run(_run())
