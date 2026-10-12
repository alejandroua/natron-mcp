"""Op registry and dispatch. Handlers receive (args: dict) and return a JSON-safe dict."""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable

from . import api_adapter as api
from . import gate as gate_mod
from .audit import audit_op

Handler = Callable[[dict[str, Any]], Any]

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()


class UnknownOp(Exception):
    pass


def _require(args: dict[str, Any], *names: str) -> None:
    missing = [n for n in names if n not in args]
    if missing:
        raise ValueError(f"missing required arg(s): {', '.join(missing)}")


def op_meta_instance_info(args: dict[str, Any]) -> dict[str, Any]:
    return api.instance_info()


def op_session_echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"echo": args}


def op_session_shutdown(args: dict[str, Any]) -> dict[str, Any]:
    from .server import request_shutdown
    request_shutdown()
    return {"shutting_down": True}


def op_project_info(args: dict[str, Any]) -> dict[str, Any]:
    return api.project_info()


def op_project_set_settings(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "settings")
    return api.project_set_settings(args["settings"])


def op_project_save(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "path")
    return api.project_save(args["path"])


def op_project_load(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "path")
    return api.project_load(args["path"])


def op_project_new(args: dict[str, Any]) -> dict[str, Any]:
    return api.project_new()


def op_nodes_list(args: dict[str, Any]) -> dict[str, Any]:
    return api.nodes_list()


def op_nodes_get(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node")
    return api.node_get(args["node"])


def op_nodes_create(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "plugin_id")
    if args.get("as_reader"):
        return api.node_create_reader(args["plugin_id"])
    if args.get("as_writer"):
        return api.node_create_writer(args["plugin_id"])
    return api.node_create(args["plugin_id"], args.get("props"))


def op_nodes_delete(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node")
    from .server import get_server
    srv = get_server()
    gui_mode = srv is not None and getattr(srv, "mode", None) == "gui"
    default_reconnect = False if gui_mode else True
    return api.node_delete(args["node"], args.get("auto_reconnect", default_reconnect))


def op_nodes_connect(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "input_index", "input_node")
    return api.node_connect(args["node"], int(args["input_index"]), args["input_node"])


def op_nodes_disconnect(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "input_index")
    return api.node_disconnect(args["node"], int(args["input_index"]))


def op_nodes_position_set(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "x", "y")
    return api.node_position_set(args["node"], args["x"], args["y"])


def op_knobs_get(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node")
    return api.knobs_get(args["node"], args.get("filter"))


def op_knobs_set(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "values")
    return api.knobs_set(args["node"], args["values"])


def op_params_expression_set(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param", "expression")
    return api.param_expression_set(args["node"], args["param"], args["expression"],
                                    args.get("has_ret", True), int(args.get("dimension", 0)))


def op_params_expression_get(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param")
    return api.param_expression_get(args["node"], args["param"], int(args.get("dimension", 0)))


def op_params_keyframe_set(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param", "time", "value")
    return api.param_keyframe_set(args["node"], args["param"], args["time"], args["value"],
                                  int(args.get("dimension", 0)))


def op_params_keyframe_remove(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param", "time")
    return api.param_keyframe_remove(args["node"], args["param"], args["time"],
                                     int(args.get("dimension", 0)))


def op_params_animated_get(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param")
    return api.param_animated_get(args["node"], args["param"], int(args.get("dimension", 0)))


def op_params_keyframes_get(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "node", "param")
    return api.keyframes_get(args["node"], args["param"], int(args.get("dimension", 0)))


def op_plugins_search(args: dict[str, Any]) -> dict[str, Any]:
    return api.plugins_search(args.get("filter", ""), int(args.get("limit", 200)))


def op_session_modal_status(args: dict[str, Any]) -> dict[str, Any]:
    return api.modal_status()


def op_session_modal_dismiss(args: dict[str, Any]) -> dict[str, Any]:
    return api.modal_dismiss(str(args.get("action", "discard")))


def op_layers_list(args: dict[str, Any]) -> dict[str, Any]:
    return api.layers_list()


def op_exec_python(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "code")
    gate = gate_mod.check_source if args.get("gate", True) else None
    try:
        return api.exec_python(args["code"], gate=gate)
    except PermissionError as exc:
        return {"status": "blocked", "result": None, "stdout": "", "stderr": "",
                "violations": str(exc).split(": ", 1)[-1].split("; ")}


def op_render_start(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "write_node", "first", "last")
    app = api.get_app()
    node = api._find_node(args["write_node"])
    step = int(args.get("step", 1))
    task_id = uuid.uuid4().hex[:12]
    record = {"task_id": task_id, "op": "render",
              "write_node": args["write_node"], "first": args["first"],
              "last": args["last"], "step": step, "started": time.time(),
              "state": "working"}
    with _tasks_lock:
        _tasks[task_id] = record
    try:
        app.render(node, int(args["first"]), int(args["last"]), step)
    except Exception:
        record["state"] = "failed"
        raise
    return {"task_id": task_id, "state": "working",
            "note": "render queued (non-blocking); poll render.status"}


def op_render_status(args: dict[str, Any]) -> dict[str, Any]:
    _require(args, "task_id")
    with _tasks_lock:
        record = _tasks.get(args["task_id"])
    if record is None:
        raise KeyError(f"unknown task: {args['task_id']}")
    out = dict(record)
    outputs = record.get("expected_outputs") or []
    if outputs:
        import os
        found = [p for p in outputs if os.path.exists(p)]
        out["outputs_found"] = len(found)
        out["outputs_total"] = len(outputs)
        if record["state"] == "working" and len(found) >= len(outputs):
            out["state"] = "completed?"
    return out


def op_tasks_list(args: dict[str, Any]) -> dict[str, Any]:
    with _tasks_lock:
        return {"tasks": [dict(v) for v in _tasks.values()]}


OPS: dict[str, Handler] = {
    "meta.instance_info": op_meta_instance_info,
    "session.echo": op_session_echo,
    "session.shutdown": op_session_shutdown,
    "session.modal_status": op_session_modal_status,
    "session.modal_dismiss": op_session_modal_dismiss,
    "project.info": op_project_info,
    "project.set_settings": op_project_set_settings,
    "project.save": op_project_save,
    "project.load": op_project_load,
    "project.new": op_project_new,
    "nodes.list": op_nodes_list,
    "nodes.get": op_nodes_get,
    "nodes.create": op_nodes_create,
    "nodes.delete": op_nodes_delete,
    "nodes.connect": op_nodes_connect,
    "nodes.disconnect": op_nodes_disconnect,
    "nodes.position_set": op_nodes_position_set,
    "knobs.get": op_knobs_get,
    "knobs.set": op_knobs_set,
    "params.expression_set": op_params_expression_set,
    "params.expression_get": op_params_expression_get,
    "params.keyframe_set": op_params_keyframe_set,
    "params.keyframe_remove": op_params_keyframe_remove,
    "params.animated_get": op_params_animated_get,
    "params.keyframes_get": op_params_keyframes_get,
    "plugins.search": op_plugins_search,
    "layers.list": op_layers_list,
    "exec.python": op_exec_python,
    "render.start": op_render_start,
    "render.status": op_render_status,
    "tasks.list": op_tasks_list,
}

READONLY_OPS = {
    "meta.instance_info", "session.echo", "session.modal_status", "project.info", "nodes.list", "nodes.get",
    "knobs.get", "params.expression_get", "params.animated_get", "params.keyframes_get",
    "plugins.search", "layers.list", "tasks.list", "render.status",
}

MUTATING_UNDOABLE = READONLY_OPS ^ set(OPS) - {"session.shutdown", "session.echo"}


def dispatch(op: str, args: dict[str, Any], readonly_mode: bool = False) -> Any:
    handler = OPS.get(op)
    if handler is None:
        raise UnknownOp(f"unknown op: {op}")
    if readonly_mode and op not in READONLY_OPS:
        raise PermissionError(f"session is readonly; denied op: {op}")
    start = time.time()
    ok = True
    detail = None
    try:
        return handler(args or {})
    except Exception as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        raise
    finally:
        audit_op(op, args, ok, (time.time() - start) * 1000.0, detail)
