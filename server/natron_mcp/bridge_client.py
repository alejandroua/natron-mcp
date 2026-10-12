"""TCP client for the in-host Natron bridge: NDJSON, request-id echo, events."""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from itertools import count
from pathlib import Path
from typing import Any, Callable

from . import config
from .protocol import (DEFAULT_OP_TIMEOUT_S, decode_frames, encode_frame,
                       make_request)


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(self, host: str | None = None, port: int | None = None):
        self.host = host or "127.0.0.1"
        self.port = port
        self._sock: socket.socket | None = None
        self._ids = count(1)
        self._lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_handlers: list[Callable[[dict[str, Any]], None]] = []
        self.hello: dict[str, Any] | None = None
        self._reader: threading.Thread | None = None

    # ---- discovery -------------------------------------------------------
    @staticmethod
    def discover_sessions() -> list[dict[str, Any]]:
        return config.discover_sessions()

    @classmethod
    def connect_auto(cls, session_id: str | None = None) -> "BridgeClient":
        sessions = cls.discover_sessions()
        if session_id:
            sessions = [s for s in sessions if s.get("session_id") == session_id]
        errors = []
        for s in sessions:
            for attempt in range(2):
                try:
                    client = cls(host=s["host"], port=int(s["port"]))
                    client.connect()
                    return client
                except OSError as exc:
                    errors.append(f"{s.get('session_id')}: {exc}")
                    break
                except BridgeError as exc:
                    errors.append(f"{s.get('session_id')}: {exc}")
                    if attempt == 1:
                        break
        if not errors:
            raise BridgeError("no reachable Natron session (0 registered)")
        raise BridgeError("no reachable Natron session; errors: " + "; ".join(errors))

    # ---- connection ------------------------------------------------------
    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, name="natron-mcp-reader",
                                        daemon=True)
        self._reader.start()
        deadline = time.time() + 5.0
        while self.hello is None and time.time() < deadline:
            time.sleep(0.02)
        if self.hello is None:
            raise BridgeError("bridge did not send hello")

    def close(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    def ensure_connected(self) -> None:
        if self._sock is None:
            self.connect()

    # ---- request/response ------------------------------------------------
    def request(self, op: str, args: dict[str, Any] | None = None,
                timeout_s: float = DEFAULT_OP_TIMEOUT_S) -> Any:
        self.ensure_connected()
        req_id = next(self._ids)
        frame = make_request(req_id, op, args, timeout_s)
        try:
            with self._lock:
                assert self._sock is not None
                self._sock.sendall(encode_frame(frame))
        except OSError:
            self.close()
            retry = BridgeClient(host=self.host, port=self.port)
            retry.connect()
            self._adopt(retry)
            return retry.request(op, args, timeout_s)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            resp = self._responses.pop(req_id, None)
            if resp is not None:
                if resp.get("ok"):
                    return resp.get("result")
                err = resp.get("error", {})
                exc = BridgeError(f"[{op}] {err.get('code')}: {err.get('message')}")
                exc.detail = err.get("detail")
                raise exc
            time.sleep(0.01)
        raise BridgeError(f"[{op}] timed out after {timeout_s}s")

    def _adopt(self, other: "BridgeClient") -> None:
        self._sock = other._sock
        self._reader = other._reader
        self._responses = other._responses
        self._events = other._events
        self.hello = other.hello

    def add_event_handler(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._event_handlers.append(fn)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._events[-limit:])

    # ---- reader ----------------------------------------------------------
    def _read_loop(self) -> None:
        buffer = b""
        try:
            while True:
                if self._sock is None:
                    return
                try:
                    data = self._sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                frames, buffer = decode_frames(buffer)
                for frame in frames:
                    if "op" in frame:
                        continue
                    if "event" in frame:
                        if frame.get("event") == "hello":
                            self.hello = frame.get("data")
                        self._events.append(frame)
                        for fn in self._event_handlers:
                            try:
                                fn(frame)
                            except Exception:
                                pass
                    elif frame.get("id") is not None:
                        self._responses[frame["id"]] = frame
        except Exception:
            return
