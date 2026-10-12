"""Bootstrap script for headless Natron sessions (NatronRenderer / natron-python).

Usage: NatronRenderer.exe bootstrap_headless.py [--mcp-port N] [--mcp-readonly]
Blocks serving MCP bridge ops on the main thread until session.shutdown.
"""
import os
import sys
from pathlib import Path

_candidates = []
_env_dir = os.environ.get("NATRON_MCP_BRIDGE_DIR")
if _env_dir:
    _candidates.append(Path(_env_dir))
if sys.argv and str(sys.argv[0]).endswith("bootstrap_headless.py"):
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


def _argv_value(flag: str, default: str) -> str:
    argv = [str(a) for a in sys.argv]
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def main() -> None:
    import os
    port = int(os.environ.get("NATRON_MCP_PORT", _argv_value("--mcp-port", "0")))
    readonly = ("--mcp-readonly" in [str(a) for a in sys.argv]
                or os.environ.get("NATRON_MCP_READONLY") == "1")
    app = globals().get("app")
    if app is None:
        try:
            import NatronEngine
            app = NatronEngine.natron.getInstance(0)
        except Exception:
            app = None
    if app is not None:
        mcp.bind_app(app)
    srv = mcp.start_headless(port=port, readonly=readonly)
    srv.serve_forever()


main()
