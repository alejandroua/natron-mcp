"""TCP bridge server: client threads do socket I/O only; ops always execute on
Natron's main thread (GUI: Qt dispatcher; headless: main-loop drain)."""
from __future__ import annotations

import os
import queue as queue_mod
import socket
import threading
import time
import traceback
import uuid
from typing import Any

from . import dispatcher, protocol, session as session_mod
from .audit import init_audit
from .protocol import decode_frames, encode_frame, make_error, make_event, make_result

_shutdown = threading.Event()
_server_ref: dict[str, Any] = {}


def _debug_trace(msg: str) -> None:
    from pathlib import Path
    marker = Path(os.environ.get(
        "NATRON_MCP_HOME", str(Path.home() / ".Natron" / "natron-mcp"))) / "gui-bootstrap-trace.log"
    try:
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write("%.2f %s\n" % (time.time(), msg))
    except Exception:
        pass

# (request_frame, response_queue) delivered to the main thread
op_queue: "queue_mod.Queue[tuple[dict[str, Any], queue_mod.Queue[dict[str, Any]]]]" = queue_mod.Queue()


def request_shutdown() -> None:
    _shutdown.set()
    srv = _server_ref.get("server")
    if srv is not None:
        srv.wake_for_shutdown()


def is_shutdown_requested() -> bool:
    return _shutdown.is_set()


def execute_op_on_main_thread(frame: dict[str, Any],
                              readonly: bool,
                              timeout_s: float) -> dict[str, Any]:
    """Called from a client I/O thread: queue the op and wait for the main thread."""
    resp_q: "queue_mod.Queue[dict[str, Any]]" = queue_mod.Queue()
    op_queue.put((frame, resp_q))
    try:
        return resp_q.get(timeout=timeout_s)
    except queue_mod.Empty:
        return make_error(frame.get("id"), "timeout",
                          f"main-thread dispatch timed out: {frame.get('op')}")


class QtDispatcher:
    """GUI mode: a persistent QTimer created ON the Qt main thread (in
    start_gui, which runs on the main thread) drains the op queue.

    The serve thread must never touch Qt: QTimer/invokeMethod from a foreign
    (non-QThread) thread recurses inside Qt6Core and segfaults.

    Re-entrancy: an op may open a modal dialog (e.g. Natron's "Save changes?"
    on project reset) whose nested event loop keeps pumping this timer while
    the op is still executing. In that state the tick (a) runs the modal
    watchdog so automation never wedges on a dialog, and (b) is allowed to
    execute further ops re-entrantly (the graph is idle while a modal waits
    for input). Any other nested tick is ignored via the drain-depth guard.
    """

    MODAL_GRACE_S = 1.5

    def __init__(self) -> None:
        from PySide6 import QtCore

        timer = QtCore.QTimer()
        timer.setInterval(100)
        timer.timeout.connect(self._drain)
        timer.start()
        self._timer = timer
        self._drain_depth = 0
        self._in_flight: dict[str, Any] | None = None
        self._modal_seen: tuple[int, float] | None = None

    def _drain(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication

            modal = QApplication.activeModalWidget()
        except Exception as exc:
            modal = None
            _debug_trace("tick error: activeModalWidget() raised %r" % (exc,))
        if os.environ.get("NATRON_MCP_DEBUG") == "1":
            _debug_trace("tick depth=%s modal=%r in_flight=%s"
                         % (self._drain_depth, modal is not None, self._in_flight is not None))
        if modal is not None:
            self._watch_modal(modal)
        if self._drain_depth > 0 and modal is None:
            return
        self._drain_depth += 1
        try:
            while True:
                try:
                    frame, resp_q = op_queue.get_nowait()
                except queue_mod.Empty:
                    return
                self._in_flight = frame
                try:
                    resp_q.put(_run_op(frame, readonly=False))
                finally:
                    self._in_flight = None
        finally:
            self._drain_depth -= 1

    def _watch_modal(self, modal: Any) -> None:
        """Auto-answer a modal dialog that an in-flight op is blocked on.

        Policy via NATRON_MCP_MODAL_POLICY: discard (default) | save | off.
        Only ever fires while an MCP op is executing (never on dialogs the
        user opened themselves while the bridge is idle).
        """
        policy = os.environ.get("NATRON_MCP_MODAL_POLICY", "discard").strip().lower()
        if policy in ("", "off", "none", "manual"):
            self._modal_seen = None
            return
        if self._in_flight is None and self._drain_depth == 0:
            self._modal_seen = None
            return
        now = time.time()
        if self._modal_seen and self._modal_seen[0] == id(modal):
            first_seen = self._modal_seen[1]
        else:
            first_seen = now
            self._modal_seen = (id(modal), first_seen)
        if now - first_seen < self.MODAL_GRACE_S:
            return
        from . import api_adapter as api

        try:
            api.modal_dismiss(policy)
        except Exception:
            pass
        finally:
            self._modal_seen = None


def _run_op(frame: dict[str, Any], readonly: bool) -> dict[str, Any]:
    req_id = frame.get("id")
    op = frame.get("op")
    args = frame.get("args") or {}
    try:
        result = dispatcher.dispatch(op, args, readonly_mode=readonly)
        return make_result(req_id, result)
    except dispatcher.UnknownOp as exc:
        return make_error(req_id, "unknown_op", str(exc))
    except PermissionError as exc:
        return make_error(req_id, "denied", str(exc))
    except Exception as exc:
        return make_error(req_id, "op_error", f"{type(exc).__name__}: {exc}",
                          traceback.format_exc(limit=6))


class BridgeServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 mode: str = "auto", readonly: bool = False):
        self.host = host
        self.requested_port = port
        self.mode = mode
        self.readonly = readonly
        self.session_id = uuid.uuid4().hex[:12]
        self.port: int | None = None
        self._sock: socket.socket | None = None
        self._wakeup_r: socket.socket | None = None
        self._wakeup_w: socket.socket | None = None
        self._qapp: Any = None
        self._qapp_tried = False
        self._qtdisp: QtDispatcher | None = None
        self._client_threads: list[threading.Thread] = []

    def start_background(self) -> int:
        self._bind()
        threading.Thread(target=self._serve_loop, name="natron-mcp-bridge", daemon=True).start()
        return self.port

    def serve_forever(self) -> None:
        self._bind()
        self._serve_loop()

    def _bind(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.requested_port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._wakeup_r, self._wakeup_w = socket.socketpair()
        session_mod.CURRENT_SESSION_ID = self.session_id
        session_mod.CURRENT_MODE = self._detect_mode()
        session_mod.register_session(self.session_info())
        init_audit(self.session_id)

    def close(self) -> None:
        session_mod.remove_session(self.session_id)
        for s in (self._wakeup_w, self._wakeup_r, self._sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    def wake_for_shutdown(self) -> None:
        try:
            if self._wakeup_w:
                self._wakeup_w.send(b"x")
        except OSError:
            pass

    def _detect_mode(self) -> str:
        try:
            import NatronGui  # noqa: F401
        except ImportError:
            return "headless"
        try:
            import natron
            gui = getattr(natron, "getGuiInstance", None)
            return "gui" if (gui and gui(0)) else "renderer"
        except Exception:
            return "renderer"

    def session_info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "host": self.host,
            "port": self.port,
            "mode": session_mod.CURRENT_MODE,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "readonly": self.readonly,
            "pid": os.getpid(),
        }

    def _pump_events(self) -> None:
        """Headless sessions have a QCoreApplication but no running event loop;
        queued work (e.g. deferred node deletion) needs manual pumping."""
        if not self._qapp_tried:
            self._qapp_tried = True
            try:
                from PySide6.QtCore import QCoreApplication
            except ImportError:
                try:
                    from PySide2.QtCore import QCoreApplication  # type: ignore
                except ImportError:
                    return
            self._qapp = QCoreApplication.instance()
        if self._qapp:
            try:
                self._qapp.processEvents()
            except Exception:
                pass

    def _drain_op_queue(self) -> int:
        """Main thread: execute queued ops. GUI delegates to Qt; headless inline."""
        n = 0
        while True:
            try:
                frame, resp_q = op_queue.get_nowait()
            except queue_mod.Empty:
                return n
            resp_q.put(_run_op(frame, readonly=self.readonly))
            n += 1

    def _serve_loop(self) -> None:
        assert self._sock is not None and self._wakeup_r is not None
        try:
            self._sock.settimeout(0.1)
            while not _shutdown.is_set():
                if self._qtdisp is None:
                    # Headless (no Qt dispatcher): the serve loop runs on the
                    # main interpreter thread, so draining here is safe and
                    # required. In GUI mode a QtDispatcher owns the queue and
                    # this thread must not touch it or Qt.
                    self._drain_op_queue()
                    self._pump_events()
                try:
                    client, _addr = self._sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                t = threading.Thread(target=self._serve_client, args=(client,),
                                     name="natron-mcp-client", daemon=True)
                self._client_threads.append(t)
                t.start()
        finally:
            self.close()

    def _serve_client(self, client: socket.socket) -> None:
        """I/O only: parse frames, queue ops, send back main-thread results."""
        try:
            client.settimeout(None)
            client.sendall(encode_frame(make_event("hello", self.session_info())))
            buffer = b""
            while not _shutdown.is_set():
                data = client.recv(65536)
                if not data:
                    break
                buffer += data
                frames, buffer = decode_frames(buffer)
                for frame in frames:
                    if "op" not in frame:
                        continue
                    timeout_s = float(frame.get("timeout_s", protocol.DEFAULT_OP_TIMEOUT_S))
                    response = execute_op_on_main_thread(frame, self.readonly, timeout_s)
                    client.sendall(encode_frame(response))
        except OSError:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            try:
                client.close()
            except OSError:
                pass

    def set_qt_dispatcher(self, qtdisp: QtDispatcher) -> None:
        self._qtdisp = qtdisp


def start_gui(port: int = 0, readonly: bool = False) -> BridgeServer:
    srv = BridgeServer(port=port, readonly=readonly)
    srv._bind()
    # CRITICAL: mark the server as GUI so the serve thread never drains ops or
    # pumps Qt events. self.mode defaults to "auto"; with "auto" the serve loop
    # would run _drain_op_queue/_pump_events on a foreign (non-QThread) thread,
    # racing the QtDispatcher timer and executing ops (or processEvents)
    # outside Natron's main thread -> SIGSEGV inside Qt6Core.
    srv.mode = "gui"
    srv.set_qt_dispatcher(QtDispatcher())
    session_mod.CURRENT_MODE = "gui"
    session_mod.register_session(srv.session_info())
    threading.Thread(target=srv._serve_loop, name="natron-mcp-bridge", daemon=True).start()
    _server_ref["server"] = srv
    return srv


def start_headless(port: int = 0, readonly: bool = False) -> BridgeServer:
    srv = BridgeServer(port=port, readonly=readonly)
    _server_ref["server"] = srv
    return srv


def get_server() -> BridgeServer | None:
    return _server_ref.get("server")
