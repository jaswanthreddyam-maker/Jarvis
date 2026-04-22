"""Lifecycle Manager — centralized start/stop/restart orchestration.

Ensures all background threads, Qt event loops, and core subsystems 
start up safely and tear down without dangling resources.
"""
from __future__ import annotations

import logging
import sys
from PySide6.QtCore import QObject

from assistant.event_bus import bus, Events

logger = logging.getLogger("Jarvis.Lifecycle")

class LifecycleManager(QObject):
    """Orchestrates system startup and shutdown safely."""

    def __init__(self, app_instance, bridge_instance) -> None:
        super().__init__()
        self._app = app_instance
        self._bridge = bridge_instance
        self._is_running = False
        
        bus.subscribe("system.recovery_requested", self._on_recovery_requested)

    def _on_recovery_requested(self, restart: bool = True, target: str = "all") -> None:
        from PySide6.QtCore import QTimer
        if target != "all":
            # Delegate partial recovery to bridge if it supports it
            if hasattr(self._bridge, "restart_module"):
                QTimer.singleShot(0, lambda: self._bridge.restart_module(target))
            else:
                logger.warning("Partial recovery requested but not supported. Falling back to full restart.")
                QTimer.singleShot(0, lambda: self.shutdown(restart=restart))
        else:
            QTimer.singleShot(0, lambda: self.shutdown(restart=restart))

    def startup(self) -> None:
        if self._is_running:
            return
        logger.info("Initializing Jarvis lifecycle...")
        self._is_running = True
        # Future system pre-checks go here
        bus.publish(Events.STATE_CHANGED, "Starting")

    def shutdown(self, restart: bool = False) -> None:
        if not self._is_running:
            return
        
        logger.info("Executing safe teardown...")
        bus.publish(Events.STATE_CHANGED, "Shutting Down")
        
        # Stop background Qt threads (Wake, ASR, TTS, Backend)
        if hasattr(self._bridge, "shutdown"):
            self._bridge.shutdown()
            
        self._is_running = False
        
        # Shutdown async execution pool
        bus.shutdown()

        if restart:
            self._do_restart()
        else:
            self._app.quit()

    def _do_restart(self) -> None:
        import subprocess
        from pathlib import Path
        
        logger.info("Restarting Jarvis process...")
        if getattr(sys, "frozen", False):
            # Running as compiled PyInstaller executable
            subprocess.Popen([sys.executable] + sys.argv[1:])
        else:
            # Running from python script
            app_script = Path(__file__).resolve().parents[1] / "ui" / "app.py"
            subprocess.Popen([sys.executable, str(app_script)] + sys.argv[1:])
        
        self._app.quit()
