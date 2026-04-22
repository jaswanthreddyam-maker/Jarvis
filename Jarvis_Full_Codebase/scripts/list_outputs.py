import sounddevice as sd
devs = sd.query_devices()
for i, d in enumerate(devs):
    if d["max_output_channels"] > 0:
        api = sd.query_hostapis(d["hostapi"])["name"]
        print(f"[{i}] {d['name'][:55]:55s} out={d['max_output_channels']} rate={d['default_samplerate']:.0f} api={api}")
