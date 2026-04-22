from __future__ import annotations


class VoiceActivityDetector:
    def trim(self, audio_chunk: bytes) -> bytes:
        """Safe passthrough return for unimplemented VAD."""
        return audio_chunk
