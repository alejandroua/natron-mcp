"""Wire protocol (shared format with the bridge; duplicated intentionally so the
server runs in its own Python environment without importing Natron code)."""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_OP_TIMEOUT_S = 30.0
MAX_FRAME_BYTES = 64 * 1024 * 1024


class ProtocolError(Exception):
    pass


def encode_frame(frame: dict[str, Any]) -> bytes:
    line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), default=str)
    data = line.encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: {len(data)} bytes")
    return data + b"\n"


def decode_frames(buffer: bytes | bytearray) -> tuple[list[dict[str, Any]], bytes]:
    frames: list[dict[str, Any]] = []
    start = 0
    while True:
        idx = buffer.find(b"\n", start)
        if idx < 0:
            break
        line = buffer[start:idx]
        start = idx + 1
        if not line.strip():
            continue
        try:
            frame = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"bad frame: {exc}") from exc
        if not isinstance(frame, dict):
            raise ProtocolError("frame must be a JSON object")
        frames.append(frame)
    return frames, bytes(buffer[start:])


def make_request(req_id: int, op: str, args: dict[str, Any] | None = None,
                 timeout_s: float = DEFAULT_OP_TIMEOUT_S) -> dict[str, Any]:
    return {"id": req_id, "op": op, "args": args or {}, "timeout_s": timeout_s}
