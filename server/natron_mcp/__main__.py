"""Entry point: `python -m natron_mcp` (stdio) or `python -m natron_mcp --http`."""
from __future__ import annotations

import sys


def main() -> None:
    if "--http" in sys.argv:
        from .mcp_server import main_http
        host = "127.0.0.1"
        port = 8765
        if "--host" in sys.argv:
            host = sys.argv[sys.argv.index("--host") + 1]
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        main_http(host, port)
        return
    import asyncio
    from .mcp_server import serve_stdio

    mcp = asyncio.run(serve_stdio())
    mcp.run()


if __name__ == "__main__":
    main()
