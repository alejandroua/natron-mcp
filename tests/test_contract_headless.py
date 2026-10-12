"""Live contract test: drives a real headless Natron via the bridge protocol.

Run with NATRON_BIN pointing at NatronRenderer.exe:
    NATRON_BIN=C:/.../NatronRenderer.exe python -m pytest tests/test_contract_headless.py -v
Skipped when NATRON_BIN is unset (pure unit runs).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent
BRIDGE_DIR = TOOLS_DIR / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))

from natron_mcp_bridge import protocol  # noqa: E402


class TinyClient:
    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(None)
        self.buf = b""
        self.events = []
        self.hello = None
        self._next = 1
        self.timeout = timeout
        hello, self.buf = self._read_frames_with_deadline(5.0)
        for f in hello:
            if f.get("event") == "hello":
                self.hello = f["data"]

    def _read_frames_with_deadline(self, seconds: float):
        self.sock.settimeout(seconds)
        try:
            while True:
                frames, self.buf = protocol.decode_frames(self.buf)
                if frames:
                    return frames, self.buf
                data = self.sock.recv(65536)
                if not data:
                    raise OSError("connection closed")
                self.buf += data
        finally:
            self.sock.settimeout(None)

    def call(self, op: str, args: dict | None = None, timeout: float | None = None):
        req_id = self._next
        self._next += 1
        frame = protocol.make_request(req_id, op, args, timeout or self.timeout)
        self.sock.sendall(protocol.encode_frame(frame))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            frames, self.buf = protocol.decode_frames(self.buf)
            for f in frames:
                if f.get("id") == req_id:
                    if f.get("ok"):
                        return f["result"]
                    raise AssertionError(f"[{op}] error: {f.get('error')}")
            if not frames:
                self.sock.settimeout(max(0.1, deadline - time.time()))
                data = self.sock.recv(65536)
                if not data:
                    raise OSError("connection closed")
                self.buf += data
                self.sock.settimeout(None)
        raise AssertionError(f"[{op}] timed out")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture(scope="module")
def natron_session():
    natron_bin = os.environ.get("NATRON_BIN")
    if not natron_bin:
        pytest.skip("NATRON_BIN not set")
    bridge_parent = BRIDGE_DIR.parent
    tmp = tempfile.mkdtemp(prefix="natron-mcp-test-")
    env = dict(os.environ)
    env.setdefault("NATRON_MCP_PORT", "0")
    env["NATRON_MCP_BRIDGE_DIR"] = str(BRIDGE_DIR)
    out_path = Path(tmp) / "natron-stdout.log"
    out_fh = open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(Path(natron_bin)),
         str(BRIDGE_DIR / "natron_mcp_bridge" / "bootstrap_headless.py")],
        cwd=str(Path(natron_bin).parent),
        env=env,
        stdout=out_fh,
        stderr=subprocess.STDOUT,
    )
    client = None
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                out_fh.close()
                out = out_path.read_text(encoding="utf-8", errors="replace")
                raise AssertionError(f"Natron exited early (rc={proc.returncode}):\n{out[-4000:]}")
            try:
                from natron_mcp_bridge import session as sess

                sessions = sess.load_sessions(check_liveness=True)
            except Exception:
                sessions = []
            if sessions:
                s = sessions[-1]
                try:
                    client = TinyClient(s["host"], s["port"])
                    break
                except OSError:
                    pass
            time.sleep(0.5)
        if client is None:
            raise AssertionError("could not connect to bridge within 60s")
        yield client
        try:
            client.call("session.shutdown", timeout=5)
        except Exception:
            pass
    finally:
        if client:
            client.close()
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            out_fh.close()
        except Exception:
            pass


def test_hello_and_instance_info(natron_session):
    assert natron_session.hello["protocol_version"] == 1
    info = natron_session.call("meta.instance_info")
    assert info.get("plugin_count") and info["plugin_count"] > 100


def test_project_info(natron_session):
    info = natron_session.call("project.info")
    assert "frameRange" in info
    assert isinstance(info.get("node_count"), int)


def test_node_lifecycle(natron_session):
    created = natron_session.call("nodes.create",
                                  {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    assert name
    got = natron_session.call("nodes.get", {"node": name})
    assert got["plugin_id"] == "net.sf.openfx.NoOpPlugin"
    assert got["knobs"]
    natron_session.call("nodes.position_set", {"node": name, "x": 123.0, "y": 45.0})
    deleted = natron_session.call("nodes.delete", {"node": name})
    assert deleted["deleted"] == name
    remaining = [n["script_name"] for n in natron_session.call("nodes.list")["nodes"]]
    assert name not in remaining


def test_reader_writer_chain(natron_session):
    tmp = tempfile.mkdtemp(prefix="natron-mcp-io-")
    seq = Path(tmp) / "input" / "plate.####.png"
    reader = natron_session.call("nodes.create", {"plugin_id": str(seq), "as_reader": True})
    writer = natron_session.call("nodes.create",
                                 {"plugin_id": str(Path(tmp) / "out" / "res.####.exr"),
                                  "as_writer": True})
    rname, wname = reader["script_name"], writer["script_name"]
    natron_session.call("nodes.connect", {"node": wname, "input_index": 0, "input_node": rname})
    wdump = natron_session.call("nodes.get", {"node": wname})
    assert wdump["inputs"].get("0") == rname
    for n in (rname, wname):
        natron_session.call("nodes.delete", {"node": n})


def test_knobs_and_expressions(natron_session):
    created = natron_session.call("nodes.create",
                                  {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    knobs = natron_session.call("knobs.get", {"node": name})
    assert knobs["knobs"]
    res = natron_session.call("knobs.set", {"node": name, "values": {"disableNode": True}})
    assert res["set"] == ["disableNode"]
    val = natron_session.call("knobs.get", {"node": name, "filter": "disableNode"})
    assert val["knobs"]["disableNode"]["value"] is True
    natron_session.call("knobs.set", {"node": name, "values": {"disableNode": False}})

    natron_session.call("exec.python",
                        {"code": "node = app.getNode('%s')\n"
                                 "p = node.createDoubleParam('mcpTest', 'MCP Test')\n"
                                 "p.setAddNewLine(False)\n"
                                 "result = p.getScriptName()" % name})
    expr = natron_session.call("params.expression_set",
                               {"node": name, "param": "mcpTest",
                                "expression": "3.5", "has_ret": False})
    assert expr["expression"] == "3.5" and expr["has_ret"] is False
    kf = natron_session.call("params.keyframe_set",
                             {"node": name, "param": "mcpTest", "time": 10, "value": 7.5})
    assert kf["keyframe"]["value"] == 7.5
    anim = natron_session.call("params.animated_get", {"node": name, "param": "mcpTest"})
    assert anim["animated"] is True
    natron_session.call("nodes.delete", {"node": name})


def test_exec_python_gated(natron_session):
    ok = natron_session.call("exec.python", {"code": "result = 6 * 7"})
    assert ok["status"] == "ok" and ok["result"] == 42
    expr = natron_session.call("exec.python", {"code": "6 * 7"})
    assert expr["status"] == "ok" and expr["result"] == 42
    blocked = natron_session.call("exec.python",
                                  {"code": "import os\nos.remove('definitely-not-there')"})
    assert blocked["status"] == "blocked"
    assert any("os.remove" in v for v in blocked["violations"])


def test_plugins_search(natron_session):
    res = natron_session.call("plugins.search", {"filter": "Cycles"})
    assert any("Cycles" in p for p in res["plugins"])
    assert res["total"] >= 1


def test_project_save_load(natron_session):
    tmp = tempfile.mkdtemp(prefix="natron-mcp-proj-")
    p = str(Path(tmp) / "test.ntp")
    saved = natron_session.call("project.save", {"path": p})
    assert saved["saved"]
    assert Path(p).exists()
    raw = Path(p).read_text(encoding="utf-8", errors="replace")
    assert "Natron" in raw or "<" in raw


def test_session_echo_and_shutdown_semantics(natron_session):
    echo = natron_session.call("session.echo", {"ping": [1, 2, 3]})
    assert echo["echo"]["ping"] == [1, 2, 3]
    with pytest.raises(AssertionError):
        natron_session.call("totally.bogus.op")
