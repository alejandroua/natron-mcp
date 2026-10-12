"""Bootstrap script for GUI Natron sessions: starts the bridge and returns,
letting the Qt event loop run. Usage: Natron.exe bootstrap_gui.py"""
import os
import sys
import time
from pathlib import Path

_step_marker = Path(os.environ.get("NATRON_MCP_HOME", str(Path.home() / ".Natron" / "natron-mcp"))) / "gui-bootstrap-trace.log"


def _trace(msg: str) -> None:
    try:
        with open(_step_marker, "a", encoding="utf-8") as fh:
            fh.write("%.2f %s\n" % (time.time(), msg))
    except Exception:
        pass


_trace("script started, argv=%r" % (sys.argv[:2],))

_candidates = []
_env_dir = os.environ.get("NATRON_MCP_BRIDGE_DIR")
if _env_dir:
    _candidates.append(Path(_env_dir))
if sys.argv and str(sys.argv[0]).endswith(("bootstrap_gui.py", "bootstrap_headless.py")):
    _candidates.append(Path(str(sys.argv[0])).resolve().parent.parent)
_candidates.append(Path.cwd())

_bridge_parent = None
for cand in _candidates:
    if (cand / "natron_mcp_bridge" / "__init__.py").exists():
        _bridge_parent = cand
        break
if _bridge_parent is None:
    raise RuntimeError("natron_mcp_bridge not found; set NATRON_MCP_BRIDGE_DIR")
if str(_bridge_parent) not in sys.path:
    sys.path.insert(0, str(_bridge_parent))

import natron_mcp_bridge as mcp  # noqa: E402


def main() -> None:
    port = int(os.environ.get("NATRON_MCP_PORT", "0"))
    readonly = os.environ.get("NATRON_MCP_READONLY") == "1"
    app = globals().get("app")
    if app is None:
        try:
            import NatronEngine
            app = NatronEngine.natron.getInstance(0)
        except Exception:
            app = None
    if app is not None:
        mcp.bind_app(app)
        _trace("app bound")
    else:
        _trace("app NOT found")
    srv = mcp.start_gui(port=port, readonly=readonly)
    _trace("bridge started on port %s mode=%s" % (srv.port, srv.session_info()["mode"]))
    try:
        app.writeToScriptEditor("[natron-mcp] bridge listening on port %d (session %s, mode %s)"
                                % (srv.port, srv.session_id, srv._detect_mode()))
    except Exception:
        print("[natron-mcp] bridge listening on port", srv.port)


main()
