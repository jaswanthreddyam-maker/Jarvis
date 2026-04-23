from __future__ import annotations


class LifecycleManager:
    def __init__(self, app, bridge) -> None:
        self._app = app
        self._bridge = bridge

    def startup(self) -> None:
        if hasattr(self._bridge, "start"):
            self._bridge.start()

    def shutdown(self, *, restart: bool = False) -> None:
        del restart
        if hasattr(self._bridge, "shutdown"):
            self._bridge.shutdown()
        if hasattr(self._app, "quit"):
            self._app.quit()

