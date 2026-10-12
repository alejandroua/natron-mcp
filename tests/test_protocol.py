import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from natron_mcp_bridge.protocol import (ProtocolError, decode_frames, encode_frame,
                                        make_error, make_event, make_request,
                                        make_result)


def test_roundtrip_single_frame():
    frame = make_request(1, "project.info", {"x": 1}, 5.0)
    buf = encode_frame(frame)
    frames, rest = decode_frames(buf)
    assert frames == [frame]
    assert rest == b""


def test_multiple_frames_and_partial():
    a = make_result(1, {"ok": True})
    b = make_error(2, "op_error", "boom")
    c = make_event("hello", {"port": 1})
    buf = encode_frame(a) + encode_frame(b)
    frames, rest = decode_frames(buf + encode_frame(c)[:5])
    assert frames == [a, b]
    assert rest == encode_frame(c)[:5]
    frames2, rest2 = decode_frames(rest + encode_frame(c)[5:])
    assert frames2 == [c]
    assert rest2 == b""


def test_unicode_and_types():
    frame = make_request(3, "nodes.create", {"label": "Schrödinger's node"})
    frames, _ = decode_frames(encode_frame(frame))
    assert frames[0]["args"]["label"] == "Schrödinger's node"


def test_frame_too_large():
    import pytest

    big = make_request(4, "exec.python", {"code": "x" * (70 * 1024 * 1024)})
    with pytest.raises(ProtocolError):
        encode_frame(big)


def test_bad_json_rejected():
    import pytest

    with pytest.raises(ProtocolError):
        decode_frames(b"{not json}\n")
