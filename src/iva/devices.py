"""`iva devices` — list audio sources/sinks (via sounddevice/PortAudio over
PipeWire) so you can set AUDIO_SOURCE / AUDIO_SINK / AUDIO_CHANNELS.

Each row shows the index and name you can pass (substring match also works),
the channel count, and which are the current defaults (*)."""
import sounddevice as sd


def print_devices():
    devs = sd.query_devices()
    try:
        din, dout = sd.default.device
    except Exception:
        din = dout = None

    print("INPUT sources (set AUDIO_SOURCE to index or name; AUDIO_CHANNELS for multi-ch):")
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            mark = " *" if i == din else "  "
            print(f"{mark}[{i}] {d['name']}  ({d['max_input_channels']} ch in @ {int(d['default_samplerate'])} Hz)")

    print("\nOUTPUT sinks (set AUDIO_SINK to index or name):")
    for i, d in enumerate(devs):
        if d["max_output_channels"] > 0:
            mark = " *" if i == dout else "  "
            print(f"{mark}[{i}] {d['name']}  ({d['max_output_channels']} ch out)")

    print("\n* = current default. WAKE_CH selects which channel to extract from a "
          "multi-channel source (e.g. 0 = first/FL on the XVF3800).")


if __name__ == "__main__":
    print_devices()
