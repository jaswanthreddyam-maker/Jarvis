"""
Jarvis Desktop Entry Point (Hardened version)
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import ctypes
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Fix library search path for some DLLs on Windows
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))
logger = logging.getLogger("Jarvis.UI")
RELAUNCH_MARKER = "JARVIS_VENV_RELAUNCHED"

from jarvis.runtime_bootstrap import ensure_project_runtime

if TYPE_CHECKING:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QSplashScreen
else:
    Qt: Any = None
    QTimer: Any = None
    QApplication: Any = None
    QSplashScreen: Any = None
    QGuiApplication: Any = None
    QPixmap: Any = None
    QColor: Any = None
    QPainter: Any = None
    QFont: Any = None
    QIcon: Any = None

# CLI Flags
_BACKGROUND = "--background" in sys.argv
_AUTOSTART = "--no-autostart" not in sys.argv

def _console(msg: str):
    print(msg, flush=True)


def _maybe_relaunch_in_project_venv() -> bool:
    if os.environ.get(RELAUNCH_MARKER) == "1":
        return False

    current = Path(sys.executable).resolve()
    try:
        runtime = ensure_project_runtime(console=_console)
    except Exception as exc:
        _console(f"[Jarvis] Runtime bootstrap failed: {exc}")
        return False

    expected = runtime.python_path.resolve()
    if current == expected:
        return False

    env = os.environ.copy()
    env[RELAUNCH_MARKER] = "1"
    _console(f"[Jarvis] Relaunching with project interpreter: {expected}")
    try:
        subprocess.Popen(
            [str(expected), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        return True
    except Exception as exc:
        _console(f"[Jarvis] Relaunch failed: {exc}")
        return False


def _load_qt_modules() -> None:
    global Qt, QTimer, QApplication, QSplashScreen, QGuiApplication, QPixmap, QColor, QPainter, QFont, QIcon
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QGuiApplication, QPixmap, QColor, QPainter, QFont, QIcon
        from PySide6.QtWidgets import QApplication, QSplashScreen
    except ImportError as e:
        _console(f"[Jarvis] Startup import error: {e}")
        sys.exit(1)

def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception as e:
            logger.warning("[App] Secondary DPI awareness attempt failed: %s", e)

def load_stylesheet(app: QApplication) -> None:
    path = PROJECT_ROOT / "ui" / "styles" / "theme.qss"
    if path.exists():
        app.setStyleSheet(path.read_text(encoding="utf-8"))


def log_runtime_diagnostics() -> None:
    executable = Path(sys.executable).resolve()
    logger.info("[App] Interpreter: %s", executable)
    logger.info("[App] Python: %s", sys.version.split()[0])

    expected_venv = (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    if expected_venv.exists() and executable != expected_venv:
        logger.warning("[App] Source run is not using the project interpreter: %s", expected_venv)

    if sys.version_info[:2] != (3, 10):
        logger.warning("[App] Jarvis is tuned for Python 3.10.x; current version is %s.", sys.version.split()[0])

    missing = [
        module_name
        for module_name in ("PySide6", "whisper", "TTS")
        if importlib.util.find_spec(module_name) is None
    ]
    if missing:
        logger.warning("[App] Current interpreter is missing modules: %s", ", ".join(missing))

class JarvisApp:
    def __init__(self):
        _load_qt_modules()
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("Jarvis")
        self.app.setOrganizationName("Jarvis")
        self.app.setQuitOnLastWindowClosed(False)
        
        self.window = None
        self.bridge = None
        self.hb_timer = None
        self.hb_count = 0
        self.state = None

    def bootstrap(self, background=False):
        enable_dpi_awareness()
        load_stylesheet(self.app)
        
        # Immediate imports to avoid late-stage failures
        from ui.state import state
        from ui.backend_bridge import JarvisBackendBridge
        from ui.main_window import JarvisMainWindow
        from jarvis.lifecycle_manager import LifecycleManager
        
        self.state = state
        self.bridge = JarvisBackendBridge(state)
        self.window = JarvisMainWindow(state, self.bridge)
        
        lifecycle = LifecycleManager(self.app, self.bridge)
        self.window.lifecycle = lifecycle
        lifecycle.startup()
        
        self.app._jarvis_bridge = self.bridge
        self.app._jarvis_window = self.window
        self.app.aboutToQuit.connect(self.bridge.shutdown)
        
        if not background:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
            _console("[Jarvis] Window initialized and shown.")

        # Heartbeat timer (persistent)
        self.hb_timer = QTimer(self.app)
        self.hb_timer.timeout.connect(self._on_heartbeat)
        self.hb_timer.start(1000)
        
        # Start bridge logic after a small delay to ensure UI is painting
        QTimer.singleShot(500, self.bridge.start)
        
        _console("[Jarvis] Ready.")
        return self.app.exec()

    def _on_heartbeat(self):
        self.hb_count += 1
        if self.hb_count % 10 == 0:
            if self.state:
                self.state.add_log(f"UI Heartbeat: {self.hb_count}", source="UI")
            print(f"[Jarvis] UI Heartbeat: {self.hb_count}", flush=True)

def main() -> None:
    if _maybe_relaunch_in_project_venv():
        return

    from jarvis.logger import setup_persistent_logging
    setup_persistent_logging()
    log_runtime_diagnostics()
    try:
        jarvis = JarvisApp()
        sys.exit(jarvis.bootstrap(background=_BACKGROUND))
    except Exception:
        logger.exception("UI bootstrap failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
