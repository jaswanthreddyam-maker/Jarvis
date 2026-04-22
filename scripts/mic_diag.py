"""Quick mic diagnostic — run this to find your actual volume levels."""
import sounddevice as sd
import numpy as np

sd.default.device = (1, 3)
dev_info = sd.query_devices(1)
max_ch = dev_info['max_input_channels']
print(f"Mic: {dev_info['name']}  Max channels: {max_ch}")
print()

print("Testing with channels=1:")
chunk = sd.rec(int(0.5 * 16000), samplerate=16000, channels=1, dtype='float32')
sd.wait()
print(f"  Shape: {chunk.shape}  Peak: {np.max(np.abs(chunk)):.6f}")
print()

print(f"Testing with channels={max_ch}:")
chunk = sd.rec(int(0.5 * 16000), samplerate=16000, channels=max_ch, dtype='float32')
sd.wait()
print(f"  Shape: {chunk.shape}")
for ch in range(max_ch):
    peak = np.max(np.abs(chunk[:, ch]))
    print(f"  Channel {ch}: Peak={peak:.6f}")

print()
print("--- Live volume monitor (speak now, 10 iterations) ---")
for i in range(10):
    chunk = sd.rec(int(0.3 * 16000), samplerate=16000, channels=1, dtype='float32')
    sd.wait()
    chunk = np.squeeze(chunk)
    vol = np.max(np.abs(chunk))
    bar = '#' * int(vol * 200)
    status = "VOICE" if vol > 0.01 else "silent"
    print(f"  [{status:6s}] Peak: {vol:.6f}  |{bar}")

print("Done.")
