"""End-to-end test of the FastMCP tool surface against a real headless Natron.

Uses the natron_session fixture (test_contract_headless.py) and an in-memory
fastmcp client, so the full chain is exercised: MCP tool -> bridge client ->
TCP -> bridge -> Natron main thread.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from test_contract_headless import natron_session  # noqa: E402,F401


def _run(coro):
    return asyncio.run(coro)


def _mcp():
    from natron_mcp.mcp_server import serve_stdio
    return _run(serve_stdio())


def _call_tool(mcp, name: str, args: dict | None = None):
    from fastmcp import Client

    async def _inner():
        async with Client(mcp) as client:
            res = await client.call_tool(name, args or {})
            return res

    return _run(_inner())


def _data(res):
    if hasattr(res, "data") and res.data is not None:
        return res.data
    return json.loads(res.content[0].text)


import json  # noqa: E402


def test_mcp_sessions_list(natron_session):
    mcp = _mcp()
    data = _data(_call_tool(mcp, "sessions_list"))
    assert data["sessions"], "no reachable session via MCP layer"


def test_mcp_project_and_nodes(natron_session):
    mcp = _mcp()
    info = _data(_call_tool(mcp, "session_info"))
    assert info["plugin_count"] > 100

    created = _data(_call_tool(mcp, "node_create", {"plugin_id": "net.sf.openfx.NoOpPlugin"}))
    name = created["script_name"]

    got = _data(_call_tool(mcp, "node_get", {"node": name}))
    assert got["knobs"]

    listed = _data(_call_tool(mcp, "nodes_list"))
    assert any(n["script_name"] == name for n in listed["nodes"])

    setres = _data(_call_tool(mcp, "knobs_set", {"node": name, "values": {"disableNode": True}}))
    assert setres["set"] == ["disableNode"]

    _data(_call_tool(mcp, "node_delete", {"node": name}))


def test_mcp_exec_python(natron_session):
    mcp = _mcp()
    ok = _data(_call_tool(mcp, "exec_python", {"code": "result = [i*i for i in range(5)]"}))
    assert ok["status"] == "ok" and ok["result"] == [0, 1, 4, 9, 16]
    blocked = _data(_call_tool(mcp, "exec_python", {"code": "import subprocess"}))
    assert blocked["status"] == "blocked"


def test_mcp_plugins_search(natron_session):
    mcp = _mcp()
    res = _data(_call_tool(mcp, "plugins_search", {"filter": "CyclesRender"}))
    assert any("CyclesRender" in p for p in res["plugins"])


def test_mcp_spawn_kill_session(natron_session):
    """Lifecycle tools: spawn a headless renderer host, drive it, kill it.

    Uses NATRON_BIN (same env as the natron_session fixture), so skipped when
    no host binary is configured. Runs last: the spawned host registers its
    own session, which must not disturb the fixture session.
    """
    import os

    if not os.environ.get("NATRON_BIN"):
        pytest.skip("NATRON_BIN not set")
    mcp = _mcp()
    spawned = _data(_call_tool(mcp, "sessions_spawn", {"mode": "renderer", "timeout": 120}))
    assert spawned.get("spawned") is True, spawned
    assert spawned.get("session_id") and spawned.get("pid")
    sid = spawned["session_id"]
    try:
        listed = _data(_call_tool(mcp, "sessions_list"))
        match = [s for s in listed["sessions"] if s["session_id"] == sid]
        assert match and match[0]["pid"] == spawned["pid"]
        info = _data(_call_tool(mcp, "session_info", {"session": sid}))
        assert info["plugin_count"] > 100
    finally:
        killed = _data(_call_tool(mcp, "session_kill", {"session_id": sid}))
        assert killed.get("killed") is True, killed
