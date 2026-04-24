import sys
import os
print(sys.version)
try:
    import whisper
    print("whisper ok")
except ImportError as e:
    print("error:", e)
