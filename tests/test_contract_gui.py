"""Live contract test against a real GUI Natron session (Natron.exe).

Run with NATRON_GUI_BIN pointing at Natron.exe:
    NATRON_GUI_BIN=C:/.../Natron.exe python -m pytest tests/test_contract_gui.py -v
A Natron window will open on the desktop while the tests run.
Skipped when NATRON_GUI_BIN is unset.

NOTE: node deletion in the GUI triggers an intermittent fork bug (deferred
destroy crashes the GUI graph, see docs/M0-VERIFICATION.md). All delete-free
tests run first; the delete test is last so a crash there cannot cascade.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent
BRIDGE_DIR = TOOLS_DIR.parent / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))

from test_contract_headless import TinyClient  # noqa: E402


@pytest.fixture(scope="module")
def gui_session():
    natron_bin = os.environ.get("NATRON_GUI_BIN")
    if not natron_bin:
        pytest.skip("NATRON_GUI_BIN not set")
    tmp = tempfile.mkdtemp(prefix="natron-mcp-gui-")
    env = dict(os.environ)
    env["NATRON_MCP_PORT"] = "0"
    env["NATRON_MCP_BRIDGE_DIR"] = str(BRIDGE_DIR)
    bin_dir = Path(natron_bin).parent
    out_path = Path(tmp) / "natron-stdout.log"
    out_fh = open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(Path(natron_bin)),
         str(BRIDGE_DIR / "natron_mcp_bridge" / "bootstrap_gui.py")],
        cwd=str(bin_dir),
        env=env,
        stdout=out_fh,
        stderr=subprocess.STDOUT,
    )
    client = None
    try:
        from natron_mcp_bridge import session as sess

        deadline = time.time() + 120
        while time.time() < deadline:
            if proc.poll() is not None:
                out_fh.close()
                out = out_path.read_text(encoding="utf-8", errors="replace")
                raise AssertionError(f"Natron GUI exited early (rc={proc.returncode}):\n{out[-4000:]}")
            sessions = [s for s in sess.load_sessions(check_liveness=True) if s.get("mode") == "gui"]
            if sessions:
                s = sessions[-1]
                try:
                    client = TinyClient(s["host"], s["port"])
                    break
                except OSError:
                    pass
            time.sleep(0.5)
        if client is None:
            raise AssertionError("no reachable GUI bridge session within 120s")
        assert client.hello["mode"] == "gui", f"expected gui session, got {client.hello}"
        last = -1
        stable = 0
        ready_deadline = time.time() + 120
        while time.time() < ready_deadline:
            info = client.call("meta.instance_info", timeout=10)
            count = info.get("plugin_count") or 0
            if count == last and count > 0:
                stable += 1
            else:
                stable = 0
            last = count
            if stable >= 3:
                break
            time.sleep(1)
        time.sleep(15)
        yield client
        try:
            client.call("session.shutdown", timeout=5)
        except Exception:
            pass
    finally:
        if client:
            client.close()
        time.sleep(1)
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            out_fh.close()
        except Exception:
            pass


def test_gui_instance_info(gui_session):
    info = gui_session.call("meta.instance_info")
    assert info["mode"] == "gui"
    assert info["version"].startswith("2.6")
    # Sanity floor for ANY host (workspace Windows build ~2348 plugins, the
    # Linux AppImage ships 562, older upstream portable builds ~539-554).
    # Catches "no plugins scanned" regressions without assuming one bundle.
    assert info["plugin_count"] > 400, f"implausibly few plugins, got {info['plugin_count']}"


def test_gui_node_create_get_position(gui_session):
    created = gui_session.call("nodes.create", {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    got = gui_session.call("nodes.get", {"node": name})
    assert got["plugin_id"] == "net.sf.openfx.NoOpPlugin"
    gui_session.call("nodes.position_set", {"node": name, "x": 50.0, "y": 60.0})
    listed = [n["script_name"] for n in gui_session.call("nodes.list")["nodes"]]
    assert name in listed


def test_gui_knobs_and_user_params(gui_session):
    created = gui_session.call("nodes.create", {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    res = gui_session.call("knobs.set", {"node": name, "values": {"disableNode": True}})
    assert res["set"] == ["disableNode"]
    val = gui_session.call("knobs.get", {"node": name, "filter": "disableNode"})
    assert val["knobs"]["disableNode"]["value"] is True
    gui_session.call("knobs.set", {"node": name, "values": {"disableNode": False}})
    code = "\n".join([
        f"node = app.getNode('{name}')",
        "p = node.createDoubleParam('guiTest', 'GUI Test')",
        "p.setAddNewLine(False)",
        "p.setValue(9.5)",
        "result = p.getValue()",
    ])
    out = gui_session.call("exec.python", {"code": code})
    assert out["status"] == "ok" and out["result"] == 9.5


def test_gui_viewer_and_gui_module_introspection(gui_session):
    out = gui_session.call("exec.python", {"code": "\n".join([
        "out = {}",
        "import NatronGui",
        "out['NatronGui'] = 'ok'",
        "gui = NatronGui.natron.getGuiInstance(0) if hasattr(NatronGui.natron, 'getGuiInstance') else None",
        "out['gui_instance'] = 'yes' if gui is not None else 'no'",
        "for name in ('getActiveViewer','getViewersList','getViewer'):",
        "    out['gui.' + name] = 'yes' if (gui is not None and hasattr(gui, name)) else 'no'",
        "for name in ('getActiveViewer','getViewersList'):",
        "    out['app.' + name] = 'yes' if hasattr(app, name) else 'no'",
        "if gui is not None and hasattr(gui, 'getViewersList'):",
        "    vs = gui.getViewersList()",
        "    out['viewer_count'] = len(vs)",
        "    if vs:",
        "        v = vs[0]",
        "        methods = [m for m in dir(v) if not m.startswith('_')]",
        "        out['viewer_methods_sample'] = methods[:40]",
        "result = out",
    ])})
    assert out["status"] == "ok", out
    res = out["result"]
    assert res["NatronGui"] == "ok"
    assert res["gui_instance"] == "yes"


def test_gui_plugins_search(gui_session):
    res = gui_session.call("plugins.search", {"filter": "CyclesRender"})
    assert any("CyclesRender" in p for p in res["plugins"])


def test_gui_project_save_load(gui_session):
    tmp = tempfile.mkdtemp(prefix="natron-mcp-gui-proj-")
    p = str(Path(tmp) / "gui-test.ntp")
    saved = gui_session.call("project.save", {"path": p})
    assert saved["saved"] and Path(p).exists()


def test_gui_node_delete_last(gui_session):
    """Runs last: deferred destroy has an intermittent fork crash (documented)."""
    created = gui_session.call("nodes.create", {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    gui_session.call("nodes.delete", {"node": name})
    time.sleep(4)
    proc_alive = True
    try:
        remaining = [n["script_name"] for n in gui_session.call("nodes.list", timeout=10)["nodes"]]
    except OSError:
        proc_alive = False
        remaining = None
    if proc_alive:
        assert name not in remaining
        print("delete survived and node removed")
    else:
        pytest.xfail("known fork bug: deferred destroy crashed the GUI (M1 fix target)")


def test_gui_project_new_with_dirty_project(gui_session):
    """Runs after delete: the save-changes modal must never wedge the bridge.

    Reproduces the 2026-10-11 Cherry Studio wedge: dirty project + project
    reset used to pop a modal 'Save changes?' that blocked the GUI main
    thread. The bridge now pre-saves to a scratch file (guard) and a modal
    watchdog auto-answers any remaining dialog, so reset completes.
    """
    created = gui_session.call("nodes.create", {"plugin_id": "net.sf.openfx.NoOpPlugin"})
    name = created["script_name"]
    gui_session.call("knobs.set", {"node": name, "values": {"disableNode": True}})
    # dirty project now; reset must complete without human interaction
    reset = gui_session.call("project.new", timeout=60)
    assert reset.get("reset") is True
    time.sleep(3)
    listed = gui_session.call("nodes.list", timeout=15)["nodes"]
    assert name not in [n["script_name"] for n in listed]
    # the bridge itself must still be responsive after the reset
    info = gui_session.call("meta.instance_info", timeout=15)
    assert info["mode"] == "gui"
