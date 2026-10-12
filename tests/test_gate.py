import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from natron_mcp_bridge.gate import check_source


def _blocked(code: str) -> bool:
    return bool(check_source(code))


def test_allowed_basic_ops():
    assert not _blocked("print(app.getNode('Blur1').getScriptName())")
    assert not _blocked("import NatronEngine\napp.createNode('fr.inria.built-in.NoOp')")
    assert not _blocked("import os\nos.path.exists('c:/x')")


def test_denied_file_deletion():
    assert _blocked("import os\nos.remove('c:/x')")
    assert _blocked("import os\nos.unlink('c:/x')")
    assert _blocked("import shutil\nshutil.rmtree('c:/x')")
    assert _blocked("open('c:/x', 'w').write('x')")
    assert not _blocked("open('c:/x', 'r').read()")


def test_denied_dynamic_execution():
    assert _blocked("eval('1+1')")
    assert _blocked("exec('x=1')")
    assert _blocked("compile('x=1', '<s>', 'exec')")
    assert _blocked("__import__('os')")
    assert _blocked("getattr(app, 'closeProject')()")
    assert _blocked("import subprocess\nsubprocess.run(['ls'])")


def test_denied_app_destructive():
    assert _blocked("app.closeProject()")
    assert _blocked("app.resetProject()")
    assert _blocked("app.exit()")


def test_denied_introspection_indirection():
    assert _blocked("x.__globals__")
    assert _blocked("type(app).__subclasses__()")


def test_homoglyph_normalized():
    assert _blocked("ｅｖａｌ('1+1')")  # fullwidth eval


def test_denied_threading_and_async():
    assert _blocked("import threading\nthreading.Thread()()")
    assert _blocked("async def f():\n    pass")


def test_network_timeout_required():
    # blocking network I/O wedges the host main thread: timeout= is mandatory
    assert _blocked("import urllib.request\nurllib.request.urlopen('http://x')")

    assert _blocked("from urllib.request import urlopen\nurlopen('http://x')")
    assert _blocked("from urllib import request\nrequest.urlopen('http://x')")
    assert _blocked("import urllib.request\nurllib.request.urlretrieve('http://x', 'f')")

    assert _blocked("import requests\nrequests.get('http://x')")
    assert _blocked("import requests\nrequests.post('http://x', json={})")
    assert not _blocked("import urllib.request\nurllib.request.urlopen('http://x', timeout=10)")
    assert not _blocked("import urllib.request\nurllib.request.urlopen('http://x', None, 5)")
    assert not _blocked("from urllib.request import urlopen\nurlopen('http://x', timeout=3)")
    assert not _blocked("import requests\nrequests.get('http://x', timeout=10)")
