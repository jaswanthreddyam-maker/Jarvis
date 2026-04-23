from __future__ import annotations

import unittest
from importlib.util import find_spec

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient
else:  # pragma: no cover - local environment fallback
    TestClient = None

from jarvis.api.app import create_app


class FakeApiApplication:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[object]] = {}

    def subscribe_runtime_event(self, event_name: str, callback) -> None:
        self._subscribers.setdefault(event_name, []).append(callback)

    def _publish(self, event_name: str, payload: dict[str, object]) -> None:
        for callback in self._subscribers.get(event_name, []):
            callback(payload)

    async def health_snapshot_async(self) -> dict[str, object]:
        return {
            "status": "ok",
            "process": {"pid": 1},
            "providers": {"openai": {"status": "configured"}},
        }

    async def handle_text_async(self, text: str, request_id: str | None = None) -> tuple[str, dict[str, object] | None]:
        resolved_request_id = str(request_id or "req-1")
        self._publish("runtime.status", {"type": "status", "request_id": resolved_request_id, "state": "thinking"})
        self._publish(
            "runtime.execution_update",
            {
                "type": "execution_update",
                "request_id": resolved_request_id,
                "tool": "open_app",
                "status": "started",
                "message": "Running open_app",
            },
        )
        self._publish(
            "runtime.response_chunk",
            {
                "type": "response_chunk",
                "request_id": resolved_request_id,
                "data": "Playing...",
                "final": True,
            },
        )
        return f"done: {text}", {"status": "completed"}

    def cancel_active(self, request_id: str | None = None) -> bool:
        return bool(request_id)

    def shutdown(self) -> None:
        return


@unittest.skipIf(TestClient is None, "fastapi is not installed in this environment")
class ApiLayerTests(unittest.TestCase):
    def test_health_endpoint_returns_status_payload(self) -> None:
        app = create_app(application_factory=FakeApiApplication)
        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("status", payload)
        self.assertIn("process", payload)
        self.assertIn("providers", payload)

    def test_websocket_streams_status_progress_and_completion(self) -> None:
        app = create_app(application_factory=FakeApiApplication)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                hello = websocket.receive_json()
                websocket.send_json(
                    {
                        "type": "subscribe",
                        "events": ["status", "execution_update", "response_chunk", "request_complete"],
                        "request_ids": ["42"],
                    }
                )
                subscribed = websocket.receive_json()
                websocket.send_json({"type": "execute", "request_id": "42", "text": "play music"})

                streamed = [hello, subscribed]
                while len(streamed) < 6:
                    streamed.append(websocket.receive_json())

        event_types = [item["type"] for item in streamed]
        self.assertIn("hello", event_types)
        self.assertIn("subscribed", event_types)
        self.assertIn("status", event_types)
        self.assertIn("execution_update", event_types)
        self.assertIn("response_chunk", event_types)
        self.assertIn("request_complete", event_types)


if __name__ == "__main__":
    unittest.main()
