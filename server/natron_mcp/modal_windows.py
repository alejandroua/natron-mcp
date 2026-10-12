"""OS-level modal dialog inspection/dismissal for Natron host processes.

Why this exists: when a Python-initiated modal dialog (e.g. Natron's
"Save changes?" on project reset, or a script-triggered file dialog) blocks
the host's main thread, NO Python code runs inside the host — not even timer
callbacks — so the in-host bridge cannot answer or self-heal. Detection and
dismissal must therefore happen from the MCP server process via window APIs.

Implementation: Win32 via ctypes (visible top-level windows of the host PID,
child button enumeration, WM_CLOSE / BM_CLICK). On non-Windows platforms this
module reports `supported=False` and callers degrade gracefully (in-host
bridge ops still cover most cases there).
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

WM_CLOSE = 0x0010
BM_CLICK = 0x00F5

_IS_WIN = sys.platform.startswith("win")
if _IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def supported() -> bool:
    return _IS_WIN


def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd: int) -> dict[str, int] | None:
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rc = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    return {"w": rc.right - rc.left, "h": rc.bottom - rc.top}


def _child_buttons(hwnd: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    @EnumChildProc
    def cb(child, _lparam):  # noqa: N803
        if user32.IsWindowVisible(child):
            text = _window_text(child)
            cls = _class_name(child)
            out.append({"hwnd": child, "text": text, "class": cls})
        return True

    user32.EnumChildWindows(hwnd, cb, None)
    return out


def list_windows(pid: int) -> list[dict[str, Any]]:
    """Visible top-level windows belonging to pid (dialog candidates)."""
    if not _IS_WIN or not pid:
        return []
    found: list[dict[str, Any]] = []

    @EnumWindowsProc
    def cb(hwnd, _lparam):  # noqa: N803
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        text = _window_text(hwnd)
        cls = _class_name(hwnd)
        r = _rect(hwnd)
        if not text and not (r and r["w"] > 0 and r["h"] > 0):
            return True
        found.append({
            "hwnd": hwnd,
            "title": text,
            "class": cls,
            "size": r,
            "buttons": [b["text"] for b in _child_buttons(hwnd) if b["text"]],
        })
        return True

    user32.EnumWindows(cb, None)
    return found


def _is_main_window(win: dict[str, Any]) -> bool:
    """The Natron main window: large client area (biggest of the set)."""
    s = win.get("size") or {}
    return (s.get("w", 0) * s.get("h", 0)) > 800 * 600


def dialog_candidates(pid: int) -> list[dict[str, Any]]:
    """Non-main visible captioned windows (likely dialogs), main window excluded."""
    wins = list_windows(pid)
    if not wins:
        return []
    mains = [w for w in wins if _is_main_window(w)]
    main_hwnds = {w["hwnd"] for w in mains[:1]}
    cands = [w for w in wins if w["hwnd"] not in main_hwnds]
    # keep windows that look like dialogs: captioned, reasonably small
    return [w for w in cands
            if w["title"] and (w.get("size") or {}).get("w", 0) and
            (w.get("size") or {}).get("w", 99999) < 1200]


def modal_status(pid: int) -> dict[str, Any]:
    if not _IS_WIN:
        return {"supported": False, "modal_active": None, "note": "os-level modal tools are Windows-only"}
    cands = dialog_candidates(pid)
    return {
        "supported": True,
        "pid": pid,
        "modal_active": bool(cands),
        "windows": [{"title": w["title"], "class": w["class"],
                     "size": w.get("size"), "buttons": w["buttons"]} for w in cands],
    }


_ACTION_KEYS = {
    "discard": ("discard", "don't save", "dont save", "no"),
    "save": ("save", "yes"),
    "cancel": ("cancel",),
    "close": ("close", "ok", "dismiss"),
}


def _pick_window(cands: list[dict[str, Any]], title_sub: str | None) -> dict[str, Any] | None:
    if title_sub:
        t = title_sub.lower()
        for w in cands:
            if t in w["title"].lower():
                return w
        return None
    return cands[0] if len(cands) == 1 else None


def modal_dismiss(pid: int, action: str = "close", title_sub: str | None = None) -> dict[str, Any]:
    """Dismiss a dialog of the host process via window messages.

    WM_CLOSE on a QDialog triggers reject/cancel — safe, cross-process.
    BM_CLICK on child buttons is attempted for save/discard, though Qt hosts
    often render buttons without native child HWNDs (WM_CLOSE then applies).
    """
    if not _IS_WIN:
        return {"dismissed": False, "supported": False,
                "note": "os-level modal tools are Windows-only"}
    cands = dialog_candidates(pid)
    if not cands:
        return {"dismissed": False, "modal_active": False}
    target = _pick_window(cands, title_sub)
    if target is None:
        return {"dismissed": False, "modal_active": True,
                "windows": [w["title"] for w in cands],
                "note": "multiple dialog windows; pass title_sub to pick one"}
    if action in ("save", "discard"):
        keys = _ACTION_KEYS[action]
        for b in _child_buttons(target["hwnd"]):
            text = b["text"].replace("&", "").strip().lower()
            if not text:
                continue
            if action == "save" and any(bad in text for bad in ("discard", "don't", "dont")):
                continue
            if any(text == k or k in text for k in keys):
                if user32.PostMessageW(b["hwnd"], BM_CLICK, 0, 0):
                    return {"dismissed": True, "method": "button_click",
                            "button": b["text"], "window": target["title"]}
    # default/close/cancel/failed-button paths: close the dialog (== cancel)
    if user32.PostMessageW(target["hwnd"], WM_CLOSE, 0, 0):
        return {"dismissed": True, "method": "wm_close", "window": target["title"],
                "note": "WM_CLOSE acts as cancel/reject on most dialogs"}
    return {"dismissed": False, "error": "PostMessage failed"}
