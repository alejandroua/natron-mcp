# Add to ~/.Natron/initGui.py to autoload the natron-mcp bridge in GUI sessions.
import sys

sys.path.insert(0, r"C:\Users\Alejandro\Documents\OpenCode_Natron3D\Natron\tools\mcp\bridge")

import natron_mcp_bridge as mcp

try:
    mcp.bind_app(app)
except NameError:
    try:
        import NatronEngine
        mcp.bind_app(NatronEngine.natron.getInstance(0))
    except Exception:
        pass

_srv = mcp.start_gui()
try:
    app.writeToScriptEditor("[natron-mcp] bridge listening on port %d (session %s)"
                            % (_srv.port, _srv.session_id))
except Exception:
    print("[natron-mcp] bridge listening on port", _srv.port)
