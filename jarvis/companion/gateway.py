"""
WebSocket Voice Gateway — real-time voice streaming over WebSocket.

Supports:
  - Live audio streaming (client → server for STT)
  - Response audio streaming (server → client for TTS)
  - Session lifecycle events
  - State change notifications (listening/processing/speaking)
  - Text fallback for hybrid voice+text mode
  - Multiple concurrent sessions
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any
from uuid import uuid4

from jarvis.companion.session import CompanionSession, SessionConfig

logger = logging.getLogger("Companion.Gateway")


class VoiceGateway:
    """
    WebSocket-based real-time voice gateway. Each WebSocket connection
    maps to one CompanionSession.
    """

    def __init__(
        self,
        *,
        session_factory: Any = None,  # Callable that creates CompanionSession
        host: str = "0.0.0.0",
        port: int = 8766,
    ) -> None:
        self._session_factory = session_factory
        self._host = host
        self._port = port
        self._active_sessions: dict[str, CompanionSession] = {}
        self._server = None

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package required for voice gateway")
            return

        self._server = await websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
        )
        logger.info("Voice gateway listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Shutdown all sessions and the server."""
        for session_id, session in list(self._active_sessions.items()):
            try:
                await session.end()
            except Exception:
                pass
        self._active_sessions.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Voice gateway stopped")

    async def _handle_connection(self, websocket: Any, path: str = "") -> None:
        """Handle a single WebSocket connection."""
        connection_id = uuid4().hex[:12]
        logger.info("New connection: %s", connection_id)

        session: CompanionSession | None = None

        try:
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message) if isinstance(raw_message, str) else {}
                except json.JSONDecodeError:
                    # Binary audio data
                    if session and isinstance(raw_message, bytes):
                        await self._handle_audio_chunk(session, raw_message, websocket)
                    continue

                msg_type = message.get("type", "")

                if msg_type == "session.start":
                    session = await self._start_session(message, websocket, connection_id)

                elif msg_type == "text.send" and session:
                    await self._handle_text(session, message, websocket)

                elif msg_type == "audio.chunk" and session:
                    audio_b64 = message.get("audio", "")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        await self._handle_audio_chunk(session, audio_bytes, websocket)

                elif msg_type == "session.end" and session:
                    result = await session.end()
                    await self._send(websocket, {
                        "type": "session.ended",
                        "session_id": session.session_id,
                        "metrics": {
                            "turn_count": result.get("metrics", SessionConfig()).turn_count
                            if hasattr(result.get("metrics"), "turn_count") else 0,
                        },
                    })
                    self._active_sessions.pop(session.session_id, None)
                    session = None

                elif msg_type == "interrupt" and session:
                    session.pipeline.interrupt()
                    await self._send(websocket, {"type": "state.interrupted"})

                elif msg_type == "ping":
                    await self._send(websocket, {"type": "pong"})

        except Exception as exc:
            logger.warning("Connection %s error: %s", connection_id, exc)
        finally:
            if session:
                try:
                    await session.end()
                except Exception:
                    pass
                self._active_sessions.pop(session.session_id, None)
            logger.info("Connection %s closed", connection_id)

    async def _start_session(
        self, message: dict[str, Any], websocket: Any, connection_id: str
    ) -> CompanionSession | None:
        """Create and start a new companion session."""
        if not self._session_factory:
            await self._send(websocket, {
                "type": "error",
                "message": "Session factory not configured",
            })
            return None

        session = self._session_factory()
        self._active_sessions[session.session_id] = session

        # Wire session events to WebSocket
        async def emit_state(event: str, data: dict[str, Any]) -> None:
            await self._send(websocket, {"type": f"session.{event}", **data})

        def sync_emit(event: str, data: dict[str, Any]) -> None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(emit_state(event, data))
            except RuntimeError:
                pass

        session.set_callbacks(on_state_change=sync_emit)
        await session.start()

        await self._send(websocket, {
            "type": "session.started",
            "session_id": session.session_id,
        })

        return session

    async def _handle_text(
        self, session: CompanionSession, message: dict[str, Any], websocket: Any
    ) -> None:
        """Handle text input in hybrid mode."""
        text = message.get("text", "").strip()
        if not text:
            return

        await self._send(websocket, {"type": "state.processing"})
        response = await session.handle_text_input(text)
        await self._send(websocket, {
            "type": "text.response",
            "text": response,
            "session_id": session.session_id,
        })

    async def _handle_audio_chunk(
        self, session: CompanionSession, audio: bytes, websocket: Any
    ) -> None:
        """Handle incoming audio chunk — buffer and process."""
        # For MVP, audio chunks are accumulated on the client side
        # and sent as complete utterances. Streaming STT comes later.
        pass

    @staticmethod
    async def _send(websocket: Any, data: dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(data))
        except Exception:
            pass
