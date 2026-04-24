from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket


def _coerce_request_id(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class BackendWorker(QObject):
    ready = Signal(object)
    response_ready = Signal(int, str, object)
    stream_update = Signal(int, str, object)
    request_failed = Signal(int, str)
    request_phase_changed = Signal(int, str, object)
    notifications_ready = Signal(object)
    runtime_feedback_ready = Signal(object)
    log = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._base_url = (os.getenv("JARVIS_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
        self._ws_url = os.getenv("JARVIS_BACKEND_WS_URL") or self._derive_ws_url(self._base_url)
        self._socket: QWebSocket | None = None
        self._cancelled_request_ids: set[int] = set()
        self._stream_buffers: dict[int, str] = {}
        self._safe_mode = False
        self._connected = False

    @Slot()
    def initialize(self) -> None:
        if self._socket is None:
            self._socket = QWebSocket()
            self._socket.connected.connect(self._on_connected)
            self._socket.disconnected.connect(self._on_disconnected)
            self._socket.textMessageReceived.connect(self._on_text_message)
            self._socket.errorOccurred.connect(self._on_error)

        if self._socket.state() == QAbstractSocket.SocketState.ConnectedState and self._connected:
            self.ready.emit({"status": "Ready", "safe_mode": self._safe_mode})
            return

        if self._socket.state() == QAbstractSocket.SocketState.ConnectingState:
            return

        self.log.emit("Backend", f"Connecting to backend websocket at {self._ws_url}.")
        self._socket.open(QUrl(self._ws_url))

    @Slot(int, str)
    def process(self, request_id: int, transcript: str) -> None:
        cleaned = transcript.strip()
        if not cleaned:
            self.request_failed.emit(request_id, "Empty transcript discarded.")
            return

        if not self._connected or self._socket is None or self._socket.state() != QAbstractSocket.SocketState.ConnectedState:
            self.request_failed.emit(request_id, "Backend websocket is offline.")
            return

        self._cancelled_request_ids.discard(request_id)
        self._stream_buffers[request_id] = ""
        self.request_phase_changed.emit(request_id, "PROCESSING", {"mode": "text", "route": "websocket"})
        self.log.emit("Backend", f"[WS] received text: {cleaned}")
        self._send(
            {
                "type": "execute",
                "request_id": str(request_id),
                "text": cleaned,
            }
        )

    def cancel_request_now(self, request_id: int | None = None) -> bool:
        if request_id is None:
            return False

        self._cancelled_request_ids.add(request_id)
        self._stream_buffers.pop(request_id, None)
        if not self._connected or self._socket is None:
            return False

        self._send({"type": "cancel", "request_id": str(request_id)})
        return True

    def confirm_step(self, confirmation_id: str, confirmed: bool = True) -> None:
        if not self._connected or self._socket is None:
            return
        self._send({
            "type": "confirm",
            "confirmation_id": str(confirmation_id),
            "confirmed": bool(confirmed)
        })


    @Slot(bool)
    def set_safe_mode(self, enabled: bool) -> None:
        self._safe_mode = bool(enabled)
        mode = "ON" if enabled else "OFF"
        self.log.emit("Backend", f"Safe mode toggle requested in UI: {mode}. Restart the backend service to apply it.")

    @staticmethod
    def _derive_ws_url(base_url: str) -> str:
        parsed = urlsplit(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/")
        ws_path = f"{path}/ws" if path else "/ws"
        return urlunsplit((scheme, parsed.netloc, ws_path, "", ""))

    def _send(self, payload: dict[str, object]) -> None:
        if self._socket is None or self._socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        self._socket.sendTextMessage(json.dumps(payload, ensure_ascii=True))

    def _on_connected(self) -> None:
        self.log.emit("Backend", f"Backend websocket connected at {self._ws_url}.")
        self._send(
            {
                "type": "subscribe",
                "events": [
                    "status",
                    "response_chunk",
                    "execution_update",
                    "request_complete",
                    "request_failed",
                ],
                "request_ids": ["*"],
            }
        )

    def _on_disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        if was_connected:
            self.ready.emit({"status": "Error", "error": "Backend websocket disconnected.", "safe_mode": self._safe_mode})
            self.log.emit("Backend", "Backend websocket disconnected.")

    def _on_error(self, error: QAbstractSocket.SocketError) -> None:
        del error
        if self._socket is None:
            return
        self._connected = False
        self.ready.emit({"status": "Error", "error": self._socket.errorString(), "safe_mode": self._safe_mode})
        self.log.emit("Backend", f"Backend websocket error: {self._socket.errorString()}")

    def _on_text_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.log.emit("Backend", f"Discarded non-JSON websocket payload: {message}")
            return
        if not isinstance(payload, dict):
            return

        message_type = str(payload.get("type", "") or "").strip().lower()
        request_id = _coerce_request_id(payload.get("request_id"))

        if message_type == "hello":
            self._connected = True
            self._safe_mode = bool(payload.get("safe_mode", False))
            self.ready.emit({"status": "Ready", "safe_mode": self._safe_mode})
            return

        if message_type == "subscribed":
            subscribed_events = ", ".join(list(payload.get("events") or []))
            self.log.emit("Backend", f"Subscribed to backend events: {subscribed_events or 'all'}")
            return

        if request_id is not None and request_id in self._cancelled_request_ids and message_type != "request_complete":
            return

        if message_type == "status" and request_id is not None:
            phase = str(payload.get("state", "") or "").strip().upper()
            self.request_phase_changed.emit(request_id, phase, payload)
            return

        if message_type == "execution_update" and request_id is not None:
            tool = str(payload.get("action", "") or payload.get("tool", "") or "").strip()
            state = str(payload.get("status", "") or "").strip().upper()
            message_text = str(payload.get("message", "") or "").strip()
            confirmation_id = str(payload.get("confirmation_id", "") or "").strip()
            
            if state:
                self.request_phase_changed.emit(request_id, "EXECUTING", payload)
            if state == "NEEDS_CONFIRMATION" and confirmation_id:
                self.runtime_feedback_ready.emit(
                    {
                        "type": "safety",
                        "safety_level": "CONFIRMATION REQUIRED",
                        "reason": message_text,
                        "activity": f"Confirm action: {tool}",
                        "confirmation_id": confirmation_id,
                    }
                )
            elif message_text:
                self.log.emit("Backend", f"[{tool or 'tool'}] {message_text}")
                self.runtime_feedback_ready.emit(
                    {
                        "type": "activity",
                        "activity": message_text,
                    }
                )
            return

        if message_type == "response_chunk" and request_id is not None:
            chunk = str(payload.get("data", "") or "").strip()
            if not chunk:
                return
            current = self._stream_buffers.get(request_id, "")
            combined = chunk if not current else f"{current} {chunk}".strip()
            self._stream_buffers[request_id] = combined
            self.stream_update.emit(request_id, combined, payload)
            return

        if message_type == "request_complete" and request_id is not None:
            self._cancelled_request_ids.discard(request_id)
            response = str(payload.get("response", "") or "").strip()
            snapshot = payload.get("task_snapshot")
            self._stream_buffers.pop(request_id, None)
            self.response_ready.emit(request_id, response, snapshot)
            return

        if message_type == "request_failed" and request_id is not None:
            self._cancelled_request_ids.discard(request_id)
            self._stream_buffers.pop(request_id, None)
            self.request_failed.emit(request_id, str(payload.get("error", "Backend request failed.") or "Backend request failed."))
            return

        if message_type == "cancel_ack":
            return

        if message_type == "error":
            self.log.emit("Backend", str(payload.get("error", "Backend websocket error.") or "Backend websocket error."))
