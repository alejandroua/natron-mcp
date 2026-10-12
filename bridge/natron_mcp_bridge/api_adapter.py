"""Adapter layer over Natron's Python API: defensive, JSON-safe operations.

All functions assume they run on Natron's main thread. Exceptions are
converted into error dicts so one bad param never breaks a whole dump.
"""
from __future__ import annotations

import io
import json
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any


class HostNotReady(Exception):
    pass


def get_app() -> Any:
    for getter in ("__natron_mcp_app__", "app"):
        val = globals().get(getter)
        if val is not None:
            return val
    try:
        import NatronEngine  # noqa: F401
        core = getattr(sys.modules.get("NatronEngine"), "natron", None)
        if core is not None:
            for meth in ("getInstance", "getAppInstance"):
                fn = getattr(core, meth, None)
                if fn:
                    try:
                        inst = fn(0)
                        if inst is not None:
                            return inst
                    except Exception:
                        pass
    except ImportError:
        pass
    raise HostNotReady("Natron app instance not found; bridge not running inside Natron")


def get_core() -> Any:
    """The PyCoreApplication singleton (NatronEngine.natron) with global functions."""
    try:
        import NatronEngine
        return getattr(NatronEngine, "natron", None)
    except ImportError:
        return None


def _safe(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return {"__error__": str(exc)}


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def instance_info() -> dict[str, Any]:
    app = get_app()
    core = get_core()
    info: dict[str, Any] = {}
    if core is not None:
        for name in ("getNatronVersionString", "getNatronDevelopmentStatus"):
            fn = getattr(core, name, None)
            if fn:
                info[name.removeprefix("getNatron").removesuffix("String").lower() or name] = _safe(fn)
        fn = getattr(core, "getPluginIDs", None)
        if fn:
            ids = _safe(fn)
            info["plugin_count"] = len(ids) if isinstance(ids, list) else None
    try:
        info["project_path"] = app.getProjectParam("projectPath").get()
    except Exception:
        info["project_path"] = None
    try:
        from . import session as _session
        info["session_id"] = getattr(_session, "CURRENT_SESSION_ID", None)
        info["mode"] = getattr(_session, "CURRENT_MODE", None)
    except Exception:
        pass
    return info


def project_info() -> dict[str, Any]:
    app = get_app()
    out: dict[str, Any] = {}
    for pname in ("frameRange", "playbackFrameRange", "frameStep", "format",
                  "fps", "projectPath"):
        p = _safe(app.getProjectParam, pname)
        if isinstance(p, dict):
            out[pname] = p
        elif p is not None:
            out[pname] = _param_value(p)
    out["views"] = _safe(app.getViewNames, default=[])
    nodes = _safe(_all_nodes, default=[])
    out["node_count"] = len(nodes) if isinstance(nodes, list) else None
    return out


def project_set_settings(settings: dict[str, Any]) -> dict[str, Any]:
    app = get_app()
    changed: list[str] = []
    errors: dict[str, str] = {}
    for pname, value in settings.items():
        p = _safe(app.getProjectParam, pname)
        if isinstance(p, dict) or p is None:
            errors[pname] = "no such project param"
            continue
        try:
            _param_set(p, value)
            changed.append(pname)
        except Exception as exc:
            errors[pname] = str(exc)
    return {"changed": changed, "errors": errors}


def project_save(path: str) -> dict[str, Any]:
    app = get_app()
    ok = _safe(app.saveProject, path, default=False)
    return {"saved": bool(ok), "path": path}


def project_load(path: str) -> dict[str, Any]:
    app = get_app()
    new_app = _safe(app.loadProject, path)
    if isinstance(new_app, dict):
        return {"loaded": False, "error": new_app["__error__"]}
    return {"loaded": True, "path": path}


def project_new() -> dict[str, Any]:
    app = get_app()
    pre: dict[str, Any] = {}
    # Natron's resetProject pops a MODAL "Save changes?" dialog when the
    # project has unsaved modifications (Gui::saveWarning), which blocks the
    # GUI main thread until a human clicks. Silently saving to a scratch file
    # first marks the project up-to-date so the dialog never appears.
    # Side effect: the project's filename association moves to the scratch
    # file; acceptable for automation, and much better than a wedged GUI.
    try:
        import tempfile
        from pathlib import Path as _Path
        scratch_dir = _Path(tempfile.gettempdir()) / "natron-mcp"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch = scratch_dir / ("pre-reset-scratch-%d.ntp" % int(time.time() * 1000))
        ok = _safe(app.saveProject, str(scratch))
        if isinstance(ok, dict):
            pre["pre_save_error"] = ok.get("__error__")
        else:
            pre["pre_saved_to"] = str(scratch) if ok else None
    except Exception as exc:
        pre["pre_save_error"] = str(exc)
    _safe(app.resetProject)
    return {"reset": True, **pre}


def _all_nodes() -> list[Any]:
    app = get_app()
    for name in ("getChildren", "getNodes"):
        fn = getattr(app, name, None)
        if fn:
            got = fn()
            if got is not None:
                return list(got)
    return []


def _find_node(name: str) -> Any:
    app = get_app()
    fn = getattr(app, "getNode", None)
    if fn:
        node = _safe(fn, name)
        if node is not None and not isinstance(node, dict):
            return node
    for node in _all_nodes():
        sn = _safe(node.getScriptName)
        if sn == name:
            return node
    raise KeyError(f"node not found: {name}")


def nodes_list() -> dict[str, Any]:
    nodes = _all_nodes()
    out = []
    for node in nodes:
        out.append({
            "script_name": _safe(node.getScriptName),
            "plugin_id": _safe(node.getPluginID),
            "label": _safe(node.getLabel) if hasattr(node, "getLabel") else None,
            "inputs": _node_inputs(node),
            "position": _safe(node.getPosition) if hasattr(node, "getPosition") else None,
        })
    return {"nodes": out}


def _node_inputs(node: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    getter = getattr(node, "getNumInputs", None) or getattr(node, "getMaxInputCount", None)
    if getter:
        count = _safe(getter)
        if isinstance(count, int):
            for i in range(count):
                inp = getattr(node, "getInput", None)
                if inp:
                    val = _safe(inp, i)
                    inputs[str(i)] = _safe(val.getScriptName) if val is not None and not isinstance(val, dict) else val
    return inputs


def node_get(name: str) -> dict[str, Any]:
    node = _find_node(name)
    out: dict[str, Any] = {
        "script_name": _safe(node.getScriptName),
        "plugin_id": _safe(node.getPluginID),
        "label": _safe(node.getLabel) if hasattr(node, "getLabel") else None,
        "inputs": _node_inputs(node),
        "position": _safe(node.getPosition) if hasattr(node, "getPosition") else None,
        "knobs": {},
    }
    params = _safe(node.getParams, default=[])
    for param in params or []:
        pname = _safe(param.getScriptName)
        if not pname or isinstance(pname, dict):
            continue
        out["knobs"][pname] = _param_dump(param)
    return out


def node_create(plugin_id: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    import NatronEngine
    app = get_app()
    ctor_map = {
        "int": getattr(NatronEngine, "IntNodeCreationProperty", None),
        "double": getattr(NatronEngine, "DoubleNodeCreationProperty", None),
        "bool": getattr(NatronEngine, "BoolNodeCreationProperty", None),
        "string": getattr(NatronEngine, "StringNodeCreationProperty", None),
    }
    kwargs = {}
    if props:
        for key, value in props.items():
            if key == "type":
                continue
            ctor = ctor_map.get(key)
            if ctor is not None:
                kwargs[key] = ctor(value)
    node = app.createNode(plugin_id, **kwargs) if kwargs else app.createNode(plugin_id)
    if isinstance(node, dict):
        raise RuntimeError(f"createNode failed: {node['__error__']}")
    if props and props.get("label"):
        try:
            lbl = node.getParam("label")
            if lbl is not None:
                lbl.set(props["label"])
        except Exception:
            pass
    return {"script_name": _safe(node.getScriptName), "plugin_id": _safe(node.getPluginID)}


def node_create_reader(filename: str) -> dict[str, Any]:
    app = get_app()
    node = _safe(app.createReader, filename)
    if isinstance(node, dict):
        raise RuntimeError(f"createReader failed: {node['__error__']}")
    return {"script_name": _safe(node.getScriptName), "plugin_id": _safe(node.getPluginID)}


def node_create_writer(filename: str) -> dict[str, Any]:
    app = get_app()
    node = _safe(app.createWriter, filename)
    if isinstance(node, dict):
        raise RuntimeError(f"createWriter failed: {node['__error__']}")
    return {"script_name": _safe(node.getScriptName), "plugin_id": _safe(node.getPluginID)}


def node_delete(name: str, auto_reconnect: bool = True) -> dict[str, Any]:
    node = _find_node(name)
    _safe(node.destroy, auto_reconnect)
    return {"deleted": name}


def node_connect(node: str, input_index: int, input_node: str) -> dict[str, Any]:
    target = _find_node(node)
    src = _find_node(input_node)
    ok = _safe(target.connectInput, input_index, src, default=False)
    if not ok:
        raise RuntimeError(f"connectInput({input_index}) failed (canConnectInput false?)")
    return {"connected": [node, input_index, input_node]}


def node_disconnect(node: str, input_index: int) -> dict[str, Any]:
    target = _find_node(node)
    _safe(target.disconnectInput, input_index)
    return {"disconnected": [node, input_index]}


def node_position_set(node: str, x: float, y: float) -> dict[str, Any]:
    target = _find_node(node)
    _safe(target.setPosition, float(x), float(y))
    return {"position": [x, y]}


def _param_dump(param: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    info["type"] = _safe(param.getTypeName)
    info["label"] = _safe(param.getLabel)
    dims = _safe(param.getNumDimensions, default=1)
    if not isinstance(dims, int) or dims < 1:
        dims = 1
    info["dimensions"] = dims
    info["value"] = _param_value(param)
    if hasattr(param, "getIsAnimated"):
        info["animated"] = _safe(param.getIsAnimated, 0)
    if hasattr(param, "getExpression"):
        expr = _safe(param.getExpression, 0, True) if False else _safe(_get_expression, param)
        info["expression"] = expr
    for meta in ("getIsVisible", "getIsEnabled"):
        if hasattr(param, meta):
            info[meta.removeprefix("getIs").lower()] = _safe(getattr(param, meta))
    return info


def _get_expression(param: Any) -> Any:
    fn = getattr(param, "getExpression", None)
    if not fn:
        return None
    has_ret = True
    try:
        expr = fn(0, has_ret)
    except TypeError:
        try:
            expr = fn(0)
        except Exception as exc:
            return {"__error__": str(exc)}
    return expr


_NO_VALUE_TYPES = ("Page", "Button", "Action", "Separator", "Group")


def _get_value(param: Any, d: int) -> Any:
    try:
        return param.getValue(d)
    except TypeError:
        return param.getValue()


def _set_value(param: Any, value: Any, d: int) -> None:
    try:
        param.setValue(value, d)
    except TypeError:
        param.setValue(value)


def _param_value(param: Any) -> Any:
    tname = _safe(param.getTypeName) or ""
    if any(t in tname for t in _NO_VALUE_TYPES):
        return None
    try:
        if "String" in tname:
            fn = getattr(param, "get", None)
            if fn:
                return _jsonable(fn())
        dims = getattr(param, "getNumDimensions", None)
        n = dims() if dims else 1
        if not isinstance(n, int) or n < 1:
            n = 1
        values = []
        for d in range(min(n, 16)):
            values.append(_jsonable(_get_value(param, d)))
        return values[0] if len(values) == 1 else values
    except Exception as exc:
        return {"__error__": str(exc)}


def _param_set(param: Any, value: Any) -> None:
    tname = _safe(param.getTypeName) or ""
    if any(t in tname for t in _NO_VALUE_TYPES):
        raise ValueError(f"knob of type {tname} has no settable value")
    if "String" in tname:
        fn = getattr(param, "set", None)
        if fn:
            fn(value)
            return
    dims = getattr(param, "getNumDimensions", None)
    n = dims() if dims else 1
    if not isinstance(n, int) or n < 1:
        n = 1
    if isinstance(value, (list, tuple)):
        for d, v in enumerate(value[:n]):
            _set_value(param, v, d)
    else:
        _set_value(param, value, 0)


def knobs_get(node: str, filter: str | None = None) -> dict[str, Any]:
    info = node_get(node)
    knobs = info.get("knobs", {})
    if filter:
        knobs = {k: v for k, v in knobs.items() if filter.lower() in k.lower()}
    return {"node": node, "knobs": knobs}


def knobs_set(node: str, values: dict[str, Any]) -> dict[str, Any]:
    target = _find_node(node)
    set_ok: list[str] = []
    errors: dict[str, str] = {}
    for pname, value in values.items():
        p = _safe(target.getParam, pname)
        if p is None or isinstance(p, dict):
            errors[pname] = "no such knob"
            continue
        try:
            _param_set(p, value)
            set_ok.append(pname)
        except Exception as exc:
            errors[pname] = str(exc)
    return {"set": set_ok, "errors": errors}


def param_expression_set(node: str, param: str, expression: str, has_ret: bool | None = None,
                         dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    if has_ret is None:
        has_ret = "ret" in expression and "=" in expression
    ok = _safe(p.setExpression, expression, has_ret, dimension, default=False)
    if not ok:
        raise RuntimeError(f"setExpression failed on {node}.{param}")
    return {"expression": expression, "has_ret": has_ret}


def param_expression_get(node: str, param: str, dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    return {"expression": _get_expression(p)}


def param_keyframe_set(node: str, param: str, time: float, value: Any,
                       dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    _safe(p.setValueAtTime, value, float(time), dimension)
    return {"keyframe": {"time": time, "value": value, "dimension": dimension}}


def param_keyframe_remove(node: str, param: str, time: float, dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    fn = getattr(p, "removeKeyframe", None) or getattr(p, "deleteValueAtTime", None)
    if not fn:
        raise RuntimeError("no keyframe removal API on this param")
    _safe(fn, float(time), dimension)
    return {"removed": {"time": time, "dimension": dimension}}


def param_animated_get(node: str, param: str, dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    fn = getattr(p, "getIsAnimated", None)
    return {"animated": bool(fn(dimension)) if fn else None}


def keyframes_get(node: str, param: str, dimension: int = 0) -> dict[str, Any]:
    target = _find_node(node)
    p = target.getParam(param)
    if p is None:
        raise KeyError(f"no such knob: {param}")
    n = _safe(p.getNumKeys, dimension, default=None)
    keys = []
    if isinstance(n, int):
        fn = getattr(p, "getKeyTime", None)
        for i in range(n):
            t = None
            if fn:
                try:
                    r = fn(i, dimension)
                    if isinstance(r, tuple):
                        t = float(r[1]) if r[0] else None
                    elif isinstance(r, (int, float)):
                        t = float(r)
                except Exception:
                    t = None
            keys.append({"index": i, "time": t,
                         "value": _jsonable(_get_value_at_time(p, t, dimension))
                         if t is not None else None})
    return {"keyframes": keys, "count": n,
            "note": None if isinstance(n, int) else "getNumKeys unavailable on this param type"}


def _get_value_at_time(param: Any, time: float, dimension: int) -> Any:
    try:
        return param.getValueAtTime(time, dimension)
    except TypeError:
        return param.getValueAtTime(time)


def plugins_search(filter: str = "", limit: int = 200) -> dict[str, Any]:
    core = get_core()
    fn = getattr(core, "getPluginIDs", None) if core else None
    if not fn:
        raise RuntimeError("getPluginIDs unavailable (PyCoreApplication missing)")
    ids = _safe(fn, filter, default=[]) if filter else _safe(fn, default=[])
    if not isinstance(ids, list):
        ids = []
    return {"plugins": ids[: max(0, int(limit))], "total": len(ids)}


def layers_list() -> dict[str, Any]:
    app = get_app()
    names = _safe(app.getViewNames, default=[])
    return {"views": names, "note": "project custom layers listed via layers_list_project"}


def _active_modal() -> tuple[Any, dict[str, Any] | None]:
    from PySide6.QtWidgets import QApplication, QPushButton

    w = QApplication.activeModalWidget()
    if w is None:
        return None, None
    buttons = []
    for b in w.findChildren(QPushButton):
        try:
            t = (b.text() or "").replace("&", "").strip()
        except Exception:
            continue
        if t:
            buttons.append(t)
    try:
        title = w.windowTitle()
    except Exception:
        title = None
    return w, {"title": title, "buttons": buttons}


def modal_status() -> dict[str, Any]:
    w, info = _active_modal()
    return {"modal_active": w is not None, **(info or {"title": None, "buttons": []})}


_MODAL_ACTION_KEYS = {
    "discard": ("discard", "don't save", "dont save", "no", "close without saving"),
    "save": ("save", "yes"),
    "cancel": ("cancel",),
    "close": ("close", "ok", "dismiss"),
}


def modal_dismiss(action: str = "discard") -> dict[str, Any]:
    key = str(action).strip().lower()
    if key not in _MODAL_ACTION_KEYS:
        raise ValueError("action must be one of: discard, save, cancel, close")
    from PySide6.QtWidgets import QPushButton

    w, info = _active_modal()
    if w is None:
        return {"dismissed": False, "modal_active": False}
    keys = _MODAL_ACTION_KEYS[key]
    clicked = None
    for b in w.findChildren(QPushButton):
        t = (b.text() or "").replace("&", "").strip().lower()
        if not t:
            continue
        if key == "save" and any(bad in t for bad in ("discard", "don't", "dont", "not save")):
            continue
        if any(t == k or k in t for k in keys):
            clicked = b
            break
    if clicked is None:
        try:
            w.close()
            return {"dismissed": True, "method": "close", "title": info and info.get("title")}
        except Exception as exc:
            return {"dismissed": False, "modal_active": True, "error": str(exc)}
    clicked.click()
    return {"dismissed": True, "method": "button", "button": (clicked.text() or "").replace("&", ""),
            "title": info and info.get("title")}


def exec_python(code: str, gate=None) -> dict[str, Any]:
    if gate is not None:
        violations = gate(code)
        if violations:
            raise PermissionError("exec_python blocked by safety gate: " + "; ".join(violations[:5]))
    app = get_app()
    ns: dict[str, Any] = {"app": app, "NatronEngine": None, "NatronGui": None, "natron": None}
    try:
        import NatronEngine
        ns["NatronEngine"] = NatronEngine
        ns.update({k: v for k, v in vars(NatronEngine).items() if not k.startswith("_")})
    except ImportError:
        pass
    try:
        import NatronGui
        ns["NatronGui"] = NatronGui
        ns.update({k: v for k, v in vars(NatronGui).items() if not k.startswith("_")})
    except ImportError:
        pass
    try:
        import natron
        ns["natron"] = natron
    except ImportError:
        pass
    out, err = io.StringIO(), io.StringIO()
    status = "ok"
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                result = eval(compile(code, "<natron-mcp-exec>", "eval"), ns)
            except SyntaxError:
                exec(compile(code, "<natron-mcp-exec>", "exec"), ns)
                result = ns.get("result")
        value = _jsonable(result)
    except Exception:
        status = "error"
        value = traceback.format_exc(limit=8)
    return {"status": status, "result": value, "stdout": out.getvalue()[-4000:],
            "stderr": err.getvalue()[-4000:]}
