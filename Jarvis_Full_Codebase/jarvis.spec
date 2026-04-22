# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Jarvis Desktop.

Build with:
    pyinstaller jarvis.spec

Or via the build script:
    python scripts/build.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH)

a = Analysis(
    [str(PROJECT_ROOT / 'ui' / 'app.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # UI assets
        (str(PROJECT_ROOT / 'ui' / 'styles'), 'ui/styles'),
        # Assistant configs
        (str(PROJECT_ROOT / 'assistant' / 'personality.json'), 'assistant'),
        (str(PROJECT_ROOT / 'assistant' / 'permissions.yaml'), 'assistant'),
        (str(PROJECT_ROOT / 'assistant' / 'retry_policy.yaml'), 'assistant'),
        # Icon
        (str(PROJECT_ROOT / 'jarvis.ico'), '.'),
    ],
    hiddenimports=[
        # PySide6
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # Audio
        'sounddevice',
        'soundfile',
        'scipy',
        'scipy.signal',
        'scipy.io',
        'scipy.io.wavfile',
        # AI / ML
        'whisper',
        'TTS',
        'TTS.api',
        'TTS.utils.manage',
        # Wake word
        'webrtcvad',
        'openwakeword',
        'openwakeword.model',
        'onnxruntime',
        # Backend
        'ollama',
        'pydantic',
        'yaml',
        'dotenv',
        'schedule',
        'psutil',
        'numpy',
        'requests',
        'bs4',
        'colorlog',
        # Project internals
        'ui',
        'ui.state',
        'ui.backend_bridge',
        'ui.main_window',
        'ui.overlay_window',
        'ui.panels',
        'ui.panels.controls_panel',
        'ui.panels.logs_panel',
        'ui.panels.metrics_panel',
        'ui.panels.settings_panel',
        'ui.panels.transcript_panel',
        'ui.widgets',
        'ui.widgets.status_badge',
        'ui.widgets.toast',
        'ui.workers',
        'ui.workers.asr_worker',
        'ui.workers.backend_worker',
        'ui.workers.listener_worker',
        'ui.workers.metrics_worker',
        'ui.workers.tts_worker',
        'assistant',
        'assistant.voice',
        'assistant.voice.tts',
        'assistant.voice.wake_listener',
        'assistant.orchestrator',
        'assistant.planner',
        'assistant.action_engine',
        'assistant.state_manager',
        'assistant.event_bus',
        'assistant.personality',
        'assistant.settings',
        'assistant.error_handler',
        'assistant.permissions',
        'assistant.scheduler',
        'assistant.contracts',
        'assistant.memory',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'IPython',
        'jupyter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Jarvis',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # --noconsole
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'jarvis.ico'),
)
