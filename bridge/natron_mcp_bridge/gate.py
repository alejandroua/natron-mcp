"""AST safety gate for exec_python (server-side copy exists too; this is authoritative)."""
from __future__ import annotations

import ast
import unicodedata

DENIED_FUNCTIONS = {
    "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "vars", "globals", "locals", "input", "breakpoint", "del",
}

DENIED_ATTR_CALLS = {
    ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"),
    ("os", "rename"), ("os", "replace"), ("os", "system"), ("os", "popen"),
    ("os", "kill"), ("os", "abort"), ("os", "execv"), ("os", "spawnv"),
    ("shutil", "rmtree"), ("shutil", "move"), ("shutil", "copy"),
    ("subprocess", "*"), ("importlib", "*"), ("ctypes", "*"),
    ("app", "closeProject"), ("app", "resetProject"), ("app", "exit"),
    ("app", "getAppInstance"), ("natron", "exit"),
}

DENIED_IMPORTS = {"subprocess", "ctypes", "importlib", "multiprocessing", "socket",
                  "asyncio", "threading", "signal"}

DENIED_NAMES = {"__subclasses__", "__globals__", "__builtins__", "__import__",
                "__reduce__", "__reduce_ex__", "__exit__", "__class__"}

WRITE_MODE_CHARS = {"w", "a", "x", "+"}

# Calls that block the host's main thread indefinitely if they hit a stalled
# network path. They must pass an explicit timeout=.
NETWORK_CALLS_NEEDING_TIMEOUT = {
    "urlopen", "urlretrieve", "get", "post", "put", "patch", "delete", "head",
    "options", "request",
}


class GateViolation(Exception):
    def __init__(self, message: str, node: ast.AST | None = None):
        super().__init__(message)
        self.node = node


def _norm(name: str) -> str:
    return unicodedata.normalize("NFKC", name)


def check_source(source: str) -> list[str]:
    """Return a list of violations. Empty list means the source is allowed."""
    try:
        tree = ast.parse(_norm(source))
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DENIED_IMPORTS:
                    violations.append(f"denied import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in DENIED_IMPORTS:
                violations.append(f"denied import: from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if _norm(fn.id) in DENIED_FUNCTIONS:
                    violations.append(f"denied call: {fn.id}()")
                elif _norm(fn.id) == "open" and _open_mode_is_write(node):
                    violations.append("denied call: open() in write mode")
                elif _norm(fn.id) == "urlopen":
                    _check_network_timeout(node, "urlopen", "", violations, plain_name=True)
                elif _norm(fn.id) == "urlretrieve":
                    violations.append(
                        "denied call: urlretrieve() has no timeout support; "
                        "use urllib.request.urlopen(..., timeout=N) instead")
            if isinstance(fn, ast.Attribute):
                base, attr = _resolve_base(fn)
                if attr == "system" or attr in {"unlink", "remove", "rmtree", "Popen", "run", "call"}:
                    if base in {"os", "shutil", "subprocess", "pathlib"}:
                        violations.append(f"denied call: {base}.{attr}()")
                if attr in NETWORK_CALLS_NEEDING_TIMEOUT:
                    if (base in {"urllib", "request"} and attr in {"urlopen", "urlretrieve"}) \
                            or base == "requests":
                        _check_network_timeout(node, base, attr, violations)
                for pair in DENIED_ATTR_CALLS:
                    if pair[1] == "*" and base == pair[0]:
                        violations.append(f"denied call: {base}.{attr}()")
                    elif base == pair[0] and attr == pair[1]:
                        violations.append(f"denied call: {base}.{attr}()")
                if fn.attr in {"__subclasses__", "__globals__", "__reduce__", "__reduce_ex__"}:
                    violations.append(f"denied attribute: {fn.attr}")
                if base == "open" or (fn.attr == "open" and base in {"Path", "p"}):
                    if _open_mode_is_write(node):
                        violations.append("denied call: open() in write mode")
        elif isinstance(node, ast.Name):
            if _norm(node.id) in DENIED_FUNCTIONS and isinstance(node.ctx, ast.Load):
                parent_call = _is_called(node, tree)
                if parent_call:
                    violations.append(f"denied call: {node.id}()")
        elif isinstance(node, ast.Attribute):
            attr = _norm(node.attr)
            if attr in DENIED_NAMES:
                violations.append(f"denied attribute: {attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            violations.append(f"denied statement: {type(node).__name__}")
        elif isinstance(node, ast.AsyncFunctionDef):
            violations.append("denied statement: async def")
        elif isinstance(node, ast.Await):
            violations.append("denied statement: await")
    return violations


def _is_called(name_node: ast.Name, tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.func is name_node:
            return True
    return False


def _resolve_base(attr: ast.Attribute) -> tuple[str, str]:
    parts: list[str] = []
    cur: ast.expr = attr
    while isinstance(cur, ast.Attribute):
        parts.append(_norm(cur.attr))
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(_norm(cur.id))
    base = parts[-1] if parts else ""
    return base, _norm(attr.attr)


def _check_network_timeout(call: ast.Call, base: str, attr: str, violations: list[str],
                           plain_name: bool = False) -> None:
    label = f"{base}.{attr}()" if base and attr else (f"{base}()" if base else f"{attr}()")
    if attr == "urlretrieve" or (plain_name and base == "urlretrieve"):
        violations.append(
            f"denied call: {label} has no timeout support; "
            "use urllib.request.urlopen(..., timeout=N) instead")
        return
    has_timeout = any(kw.arg == "timeout" for kw in call.keywords)
    if not has_timeout and (base in {"urllib", "request"} or not base) and len(call.args) >= 3:
        has_timeout = True  # urlopen(url, data, timeout) positional form
    if not has_timeout:
        violations.append(
            f"denied call: {label} without timeout= — blocking network I/O can wedge "
            "Natron's main thread for minutes; pass an explicit timeout (e.g. timeout=10)")


def _open_mode_is_write(call: ast.Call) -> bool:
    if len(call.args) >= 2:
        mode_arg = call.args[1]
        if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
            return any(ch in WRITE_MODE_CHARS for ch in mode_arg.value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return any(ch in WRITE_MODE_CHARS for ch in kw.value.value)
    return False
