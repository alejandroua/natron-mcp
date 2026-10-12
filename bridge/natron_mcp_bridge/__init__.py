"""natron_mcp_bridge: in-host MCP bridge addon for Natron (GUI + headless)."""
from __future__ import annotations

from typing import Any

from . import api_adapter, protocol

__version__ = "0.1.0"


def bind_app(app: Any) -> None:
    """Bind the Natron app instance (call from initGui.py / bootstrap scripts)."""
    api_adapter.__dict__["__natron_mcp_app__"] = app


def start_gui(port: int = 0, readonly: bool = False):
    """Start the bridge inside a GUI Natron session (must run on the main thread)."""
    from .server import start_gui as _start
    return _start(port=port, readonly=readonly)


def get_server():
    """Return the running BridgeServer in this session, if any (None otherwise)."""
    from .server import get_server as _get
    return _get()


def start_headless(port: int = 0, readonly: bool = False):
    """Create the bridge for a headless session; call serve_forever() to run."""
    from .server import start_headless as _start
    return _start(port=port, readonly=readonly)


def serve_headless_blocking(port: int = 0, readonly: bool = False) -> None:
    """Convenience for headless scripts: bind, register, then block until shutdown."""
    srv = start_headless(port=port, readonly=readonly)
    srv.serve_forever()
