from __future__ import annotations

import unittest

from assistant.event_bus import EventBus
from assistant.streaming_response_handler import StreamUpdate, StreamingResponseHandler


class StreamingResponseHandlerTests(unittest.TestCase):
    def test_formats_progress_updates(self) -> None:
        bus = EventBus()
        self.addCleanup(bus.shutdown)
        handler = StreamingResponseHandler(bus)
        updates: list[StreamUpdate] = []
        handler.subscribe(updates.append)

        bus.publish(
            "execution.step_start",
            {
                "task_id": "task-1",
                "step_id": "task-1-step-1",
                "step_index": 0,
                "step_total": 2,
                "action": "open_app",
                "description": "Open the chrome application.",
            },
        )
        bus.publish(
            "execution.step_done",
            {
                "task_id": "task-1",
                "step_id": "task-1-step-1",
                "message": "Launched chrome.",
            },
        )

        self.assertGreaterEqual(len(updates), 2)
        self.assertIn("Opening", updates[0].text)
        self.assertIn("Launched chrome.", updates[-1].text)

    def test_emits_cancellation_progress_and_final_stop(self) -> None:
        bus = EventBus()
        self.addCleanup(bus.shutdown)
        handler = StreamingResponseHandler(bus)
        updates: list[StreamUpdate] = []
        handler.subscribe(updates.append)

        bus.publish(
            "execution.step_start",
            {
                "task_id": "task-2",
                "description": "Open the chrome application.",
            },
        )
        bus.publish("execution.cancel_requested", {"task_id": "task-2"})
        bus.publish("execution.interrupted", {"task_id": "task-2"})

        self.assertIn("Stopping...", updates[-2].text)
        self.assertTrue(updates[-1].final)
        self.assertIn("Stopped.", updates[-1].text)


if __name__ == "__main__":
    unittest.main()
