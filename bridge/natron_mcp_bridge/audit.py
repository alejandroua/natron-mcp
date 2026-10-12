"""Append-only audit log (JSONL), one line per operation."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .session import SESSIONS_DIR

_lock = threading.Lock()
_path: Path | None = None


def init_audit(session_id: str) -> Path:
    global _path
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _path = SESSIONS_DIR / f"audit-{session_id}.jsonl"
    return _path


def audit_op(op: str, args: Any, ok: bool, duration_ms: float,
             detail: str | None = None) -> None:
    if _path is None:
        return
    record: dict[str, Any] = {
        "ts": round(time.time(), 3),
        "op": op,
        "ok": ok,
        "duration_ms": round(duration_ms, 2),
    }
    if detail:
        record["detail"] = detail[:2000]
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        with open(_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
