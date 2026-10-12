"""Configuration and session discovery for the natron-mcp server."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path(os.environ.get("NATRON_MCP_HOME", str(Path.home() / ".Natron" / "natron-mcp")))
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"


def discover_sessions() -> list[dict[str, Any]]:
    """Return registered sessions, verified by an actual TCP connect."""
    if not SESSIONS_FILE.exists():
        return []
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        sessions = list(data.get("sessions", []))
    except (OSError, json.JSONDecodeError):
        return []
    alive = []
    import socket
    for s in sessions:
        host, port = s.get("host"), s.get("port")
        if not isinstance(port, int) or not isinstance(host, str):
            continue
        try:
            with socket.create_connection((host, port), timeout=1.0):
                alive.append(s)
        except OSError:
            continue
    return alive


def http_token() -> str:
    """Token for the HTTP transport (per-session token file; generated on first use)."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    token_file = SESSIONS_DIR / "http-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    import secrets
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        pass
    return token
