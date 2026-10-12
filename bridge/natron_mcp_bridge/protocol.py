"""NDJSON wire protocol for the Natron MCP bridge."""
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


def make_result(req_id: int, result: Any) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "result": result}


def make_error(req_id: int, code: str, message: str, detail: Any = None) -> dict[str, Any]:
    err = {"code": code, "message": message}
    if detail is not None:
        err["detail"] = detail
    return {"id": req_id, "ok": False, "error": err}


def make_event(event: str, data: Any) -> dict[str, Any]:
    return {"event": event, "data": data}


def is_request(frame: dict[str, Any]) -> bool:
    return "op" in frame and "id" in frame


def is_event(frame: dict[str, Any]) -> bool:
    return "event" in frame
