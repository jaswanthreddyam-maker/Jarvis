import sounddevice as sd
import numpy as np

# Test multiple input devices to find cleanest signal
for dev_id in [0, 1, 5, 9]:
    try:
        dev = sd.query_devices(dev_id)
        if dev["max_input_channels"] < 1:
            continue
        rate = int(dev["default_samplerate"])
        name = dev["name"][:50]
        rec = sd.rec(int(2 * rate), samplerate=rate, channels=1, dtype="float32", device=dev_id)
        sd.wait()
        audio = np.squeeze(rec)
        mean_val = np.mean(np.abs(audio))
        p95 = np.percentile(np.abs(audio), 95)
        peak = np.max(np.abs(audio))
        rms = np.sqrt(np.mean(audio**2))
        print(f"[{dev_id}] {name}")
        print(f"     rate={rate}  mean={mean_val:.5f}  p95={p95:.5f}  peak={peak:.5f}  rms={rms:.5f}")
    except Exception as e:
        print(f"[{dev_id}] FAILED: {e}")
