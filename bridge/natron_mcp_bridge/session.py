"""Session registry: discovery file so the MCP server can find running hosts."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path(os.environ.get("NATRON_MCP_HOME", str(Path.home() / ".Natron" / "natron-mcp")))
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"


def ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)





def register_session(info: dict[str, Any]) -> None:
    """Register a session, pruning entries whose sockets no longer accept connections."""
    ensure_dirs()
    sessions = load_sessions(check_liveness=True)
    kept = [s for s in sessions if s.get("session_id") != info.get("session_id")]
    kept.append(info)
    _atomic_write(SESSIONS_FILE, {"sessions": kept})


def remove_session(session_id: str) -> None:
    sessions = load_sessions(check_liveness=False)
    kept = [s for s in sessions if s.get("session_id") != session_id]
    _atomic_write(SESSIONS_FILE, {"sessions": kept})


def load_sessions(check_liveness: bool = True) -> list[dict[str, Any]]:
    if not SESSIONS_FILE.exists():
        return []
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        sessions = list(data.get("sessions", []))
    except (OSError, json.JSONDecodeError):
        return []
    if check_liveness:
        sessions = [s for s in sessions if _socket_alive(s.get("host"), s.get("port"))]
    return sessions


def _socket_alive(host: Any, port: Any) -> bool:
    import socket

    if not isinstance(port, int) or not isinstance(host, str):
        return False
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _atomic_write(path: Path, data: Any) -> None:
    ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
