from __future__ import annotations

from jarvis.application.runtime_support import EventBus as _EventBus


class Events:
    STATE_CHANGED = "state.status_changed"
    WAKE_DETECTED = "wake.detected"
    TTS_STARTED = "tts.started"
    ERROR_OCCURRED = "system.error_occurred"
    PERF_MODE_CHANGED = "state.perf_mode_changed"
    METRICS_UPDATED = "metrics.updated"
    CONFIG_RELOADED = "system.config_reloaded"
    MEMORY_UPDATED = "memory.graph_updated"
    VOICE_VERIFIED = "voice.identity_verified"
    VOICE_ENROLLED = "voice.identity_enrolled"
    VOICE_GUEST = "voice.guest_detected"
    PREDICTION_READY = "proactive.prediction_ready"
    SUGGESTION_READY = "proactive.suggestion"
    AUTO_ACT_TRIGGERED = "proactive.auto_act"


bus = _EventBus()

