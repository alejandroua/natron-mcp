"""Natron host process lifecycle for MCP sessions: spawn, kill, restart.

Mirrors blender-mcp's convenience: the agent can start a fresh host, kill a
wedged one, or restart it without any manual steps. Cross-platform process
control with a minimal, justified amount of OS-specific code (taskkill on
Windows, SIGTERM/SIGKILL elsewhere).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config


class HostProcessError(Exception):
    pass


def _bridge_dir() -> Path:
    env = os.environ.get("NATRON_MCP_BRIDGE_DIR")
    if env and (Path(env) / "natron_mcp_bridge" / "__init__.py").exists():
        return Path(env)
    here = Path(__file__).resolve().parent          # .../tools/mcp/server/natron_mcp
    cand = here.parent.parent / "bridge"            # .../tools/mcp/bridge
    if (cand / "natron_mcp_bridge" / "__init__.py").exists():
        return cand
    # Installed via tools/mcp/install.py: pointer + registry written by the installer.
    for extra in (config.SESSIONS_DIR / "bridge-dir.txt",
                  Path.home() / ".Natron" / "natron-mcp" / "bridge-dir.txt"):
        try:
            if extra.exists():
                cand = Path(extra.read_text(encoding="utf-8").strip())
                if (cand / "natron_mcp_bridge" / "__init__.py").exists():
                    return cand
        except OSError:
            continue
    for entry in _registry_installs():
        cand = Path(str(entry.get("bridge_dir", "")))
        if str(cand) != "." and (cand / "natron_mcp_bridge" / "__init__.py").exists():
            return cand
    raise HostProcessError(
        "natron_mcp_bridge package not found; set NATRON_MCP_BRIDGE_DIR or run "
        "natron-mcp's install.py against your Natron")


def _registry_installs() -> list[dict[str, Any]]:
    """Installs registered by the installer (~/.Natron/natron-mcp/install.json)."""
    reg = (config.SESSIONS_DIR / "install.json")
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
        return [e for e in data.get("installs", []) if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _workspace_root() -> Path:
    """Directory that may hold sibling Natron-install/ or Natron-release/ trees.

    NATRON_MCP_WORKSPACE env overrides. Otherwise: works for both the fork's
    tools/mcp layout (4 levels up from bridge/) and this standalone repo
    layout (the repo root, its parent, or grandparent may sit next to the
    Natron install)."""
    env = os.environ.get("NATRON_MCP_WORKSPACE")
    if env and Path(env).is_dir():
        return Path(env)
    x = _bridge_dir().parent                     # <repo>/bridge (or tools/mcp/bridge)
    for cand in (x.parent, x.parent.parent, x.parent.parent.parent):
        if (cand / "Natron-install").exists() or (cand / "Natron-release").exists():
            return cand
    return x.parent.parent.parent


def resolve_binary(mode: str) -> Path:
    """Locate the Natron binary for the requested mode."""
    mode = (mode or "gui").strip().lower()
    if mode not in ("gui", "renderer", "python"):
        raise HostProcessError("mode must be one of: gui, renderer, python")
    candidates: list[Path] = []
    key = "gui_bin" if mode == "gui" else "renderer_bin"
    if mode == "gui":
        for var in ("NATRON_MCP_GUI_BIN", "NATRON_GUI_BIN"):
            v = os.environ.get(var)
            if v:
                candidates.append(Path(v))
    else:
        for var in ("NATRON_MCP_RENDERER_BIN", "NATRON_BIN"):
            v = os.environ.get(var)
            if v:
                candidates.append(Path(v))
    # Installer-registered installs (portable roots) first, then workspace guesses.
    for entry in _registry_installs():
        v = entry.get(key)
        if v:
            candidates.append(Path(str(v)))
    try:
        ws = _workspace_root()
        if mode == "gui":
            candidates += [
                ws / "Natron-install" / "App" / "Natron.exe",
                ws / "Natron-install" / "App" / "Natron",
                ws / "Natron-release" / "bin" / "Natron.exe",
                ws / "Natron-release" / "bin" / "Natron",
                ws / "Natron-release" / "usr" / "bin" / "Natron",
            ]
        else:
            candidates += [
                ws / "Natron-install" / "Renderer" / "NatronRenderer.exe",
                ws / "Natron-install" / "Renderer" / "NatronRenderer",
                ws / "Natron-release" / "bin" / "NatronRenderer.exe",
                ws / "Natron-release" / "bin" / "NatronRenderer",
                ws / "Natron-release" / "usr" / "bin" / "NatronRenderer",
            ]
    except HostProcessError:
        pass
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    raise HostProcessError(
        "no Natron binary found for mode=%r; set NATRON_MCP_GUI_BIN / "
        "NATRON_MCP_RENDERER_BIN (tried: %s)" % (mode, ", ".join(str(c) for c in candidates)))


def modal_policy() -> str:
    """Modal auto-answer policy: discard (default) | save | off (env NATRON_MCP_MODAL_POLICY)."""
    p = os.environ.get("NATRON_MCP_MODAL_POLICY", "discard").strip().lower()
    return p if p in ("discard", "save", "off") else "discard"


def _raw_sessions() -> list[dict[str, Any]]:
    if not config.SESSIONS_FILE.exists():
        return []
    try:
        data = json.loads(config.SESSIONS_FILE.read_text(encoding="utf-8"))
        return list(data.get("sessions", []))
    except (OSError, json.JSONDecodeError):
        return []


def _find_session(session_id: str, require_alive: bool) -> dict[str, Any]:
    if require_alive:
        for s in config.discover_sessions():
            if s.get("session_id") == session_id:
                return s
        raise HostProcessError(f"no reachable session: {session_id}")
    for s in _raw_sessions():
        if s.get("session_id") == session_id:
            return s
    raise HostProcessError(f"no registered session: {session_id}")


def _process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                             capture_output=True, text=True).stdout
        return any(line.lower().startswith('"') for line in out.splitlines())
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid: int, timeout: float = 10.0) -> dict[str, Any]:
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"killed": True, "pid": pid, "note": "already gone"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _process_alive(pid):
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return {"killed": True, "pid": pid}


def spawn_host(mode: str = "gui", timeout: float = 120.0,
               wait_ready_s: float = 20.0) -> dict[str, Any]:
    """Start a new Natron host with the bridge loaded; wait for registration."""
    binary = resolve_binary(mode)
    bridge = _bridge_dir()
    bootstrap = bridge / "natron_mcp_bridge" / (
        "bootstrap_gui.py" if mode == "gui" else "bootstrap_headless.py")
    env = dict(os.environ)
    env["NATRON_MCP_PORT"] = "0"
    env["NATRON_MCP_BRIDGE_DIR"] = str(bridge)
    # NATRON_MCP_MODAL_POLICY (if set in the server's environment) is inherited
    # by the spawned host on purpose: one place to configure dialog policy.
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.SESSIONS_DIR / ("spawn-%s-%d.log" % (mode, int(time.time() * 1000)))
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(binary), str(bootstrap)],
                            cwd=str(binary.parent), env=env,
                            stdout=log_fh, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise HostProcessError(
                    "Natron exited during startup (rc=%s); log: %s"
                    % (proc.returncode, _log_tail(log_path)))
            for s in config.discover_sessions():
                if s.get("pid") == proc.pid:
                    # settle: fresh GUI sessions are unsafe to mutate during
                    # late init (see M0-VERIFICATION); wait for plugin count
                    # to stabilize when possible
                    _wait_ready(s, wait_ready_s)
                    return {"spawned": True, "mode": mode, "pid": proc.pid,
                            "session_id": s.get("session_id"),
                            "host": s.get("host"), "port": s.get("port"),
                            "log": str(log_path)}
            time.sleep(0.5)
        raise HostProcessError(
            "no session registered within %.0fs; log: %s" % (timeout, _log_tail(log_path)))
    except Exception:
        if proc.poll() is None:
            try:
                _kill_pid(proc.pid)
            except Exception:
                pass
        raise
    finally:
        log_fh.close()


def _wait_ready(session: dict[str, Any], wait_ready_s: float) -> None:
    """Poll meta.instance_info until the plugin count stabilizes.

    If the fresh host pops a startup modal (e.g. the autosave-restore prompt
    after a previous crash), the bridge cannot answer; auto-dismiss the dialog
    via OS-level window messages per the modal policy, then keep waiting.
    """
    if wait_ready_s <= 0:
        return
    from .bridge_client import BridgeClient
    from .modal_windows import dialog_candidates, modal_dismiss

    policy = modal_policy()
    deadline = time.time() + wait_ready_s
    last = -1
    stable = 0
    dismissed_once = False
    while time.time() < deadline and stable < 2:
        try:
            c = BridgeClient(host=session["host"], port=int(session["port"]))
            c.connect()
            info = c.request("meta.instance_info", timeout_s=5.0)
            c.close()
            count = info.get("plugin_count") or 0
            if count == last and count > 0:
                stable += 1
            else:
                stable = 0
            last = count
        except Exception:
            if policy != "off" and not dismissed_once:
                try:
                    pid = session.get("pid")
                    cands = dialog_candidates(int(pid)) if pid else []
                    if cands:
                        res = modal_dismiss(int(pid), policy)
                        dismissed_once = True
                        _log_spawn(session, "auto-dismissed startup dialog per policy=%s: %s"
                                   % (policy, res))
                except Exception:
                    pass
            time.sleep(1.0)
            continue
        time.sleep(0.5)


def _log_spawn(session: dict[str, Any], msg: str) -> None:
    from .mcp_server import _watchdog_log
    try:
        _watchdog_log("spawn %s: %s" % (session.get("session_id"), msg))
    except Exception:
        pass


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<no log>"


def kill_session(session_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Terminate the host process behind a session (alive or stale registry)."""
    try:
        s = _find_session(session_id, require_alive=False)
    except HostProcessError:
        return {"killed": False, "session_id": session_id,
                "note": "session not registered"}
    pid = s.get("pid")
    alive = _process_alive(pid)
    if not alive:
        return {"killed": True, "session_id": session_id, "pid": pid,
                "note": "process already dead; registry entry pruned on next discovery"}
    result = _kill_pid(int(pid), timeout)
    # prune from the registry right away
    _prune_session(session_id)
    return {"session_id": session_id, **result}


def _prune_session(session_id: str) -> None:
    sessions = [s for s in _raw_sessions() if s.get("session_id") != session_id]
    try:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": sessions}, indent=2), encoding="utf-8")
        os.replace(tmp, config.SESSIONS_FILE)
    except OSError:
        pass


def restart_session(session_id: str = "", mode: str = "", timeout: float = 120.0) -> dict[str, Any]:
    """Kill the given host (best effort) and spawn a fresh one.

    mode: "gui" | "renderer" | "python"; empty means reuse the old session's mode.
    """
    killed: dict[str, Any] = {"killed": False}
    if session_id:
        try:
            killed = kill_session(session_id)
        except HostProcessError as exc:
            killed = {"killed": False, "note": str(exc)}
        if not mode:
            mode = str(_raw_mode_of(session_id) or "gui").lower()
    if not mode:
        mode = "gui"
    spawned = spawn_host(mode, timeout)
    return {"restarted": True, "previous": {"session_id": session_id, **killed},
            "session": spawned}


def _raw_mode_of(session_id: str) -> str | None:
    for s in _raw_sessions():
        if s.get("session_id") == session_id:
            return s.get("mode")
    return None
