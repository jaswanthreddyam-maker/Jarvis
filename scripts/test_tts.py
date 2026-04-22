"""Quick TTS playback test — speaks a sentence and prints debug info."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import os
os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = r"C:\Program Files\eSpeak NG\libespeak-ng.dll"
os.environ["PATH"] = r"C:\Program Files\eSpeak NG;" + os.environ.get("PATH", "")

from assistant.voice.tts import TTS

print("=" * 40)
print("  TTS PLAYBACK TEST")
print("=" * 40)

tts = TTS()
print("\nSpeaking test phrase (blocking)...")
tts.speak("Hello, this is a test of the Jarvis voice output system.", blocking=True)
print("\nDone. Did you hear it?")
