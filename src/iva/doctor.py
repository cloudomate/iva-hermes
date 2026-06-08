"""`iva doctor` — verify runtime dependencies (Python packages AND the system
tools/libraries pip can't install) and report what's missing + how to fix it.

`iva run` calls preflight() first so it fails fast with a clear message instead
of a deep traceback when something is missing.
"""
import importlib
import importlib.util
import shutil
import sys

# (label, import-name)  — real import so a missing C lib (PortAudio/libsndfile)
# is caught, not just the .py file being present.
_PY_HARD = [
    ("numpy", "numpy"),
    ("sounddevice", "sounddevice"),
    ("soundfile", "soundfile"),
    ("pymicro-wakeword", "pymicro_wakeword"),
]
_PY_SOFT = [("rich (for `iva display`)", "rich")]
# hermes-agent: check presence without importing the whole agent.
_TOOL_HARD = [("ffmpeg (mp3 TTS playback)", "ffmpeg")]
_TOOL_SOFT = [("wpctl (`iva volume`)", "wpctl"), ("pactl (PipeWire)", "pactl")]

_FIX = {
    "numpy": "pip install iva-hermes",
    "sounddevice": "pip install iva-hermes  (+ sudo apt install libportaudio2)",
    "soundfile": "pip install iva-hermes  (+ sudo apt install libsndfile1)",
    "pymicro-wakeword": "pip install iva-hermes",
    "hermes-agent": "pip install hermes-agent",
    "ffmpeg (mp3 TTS playback)": "sudo apt install ffmpeg",
    "wpctl (`iva volume`)": "sudo apt install wireplumber",
    "pactl (PipeWire)": "sudo apt install pipewire-pulse",
    "rich (for `iva display`)": "pip install 'iva-hermes[display]'",
}


def _can_import(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def diagnose():
    """Return rows of (label, kind, ok, required)."""
    rows = []
    for label, mod in _PY_HARD:
        rows.append((label, "python", _can_import(mod), True))
    # hermes-agent: just needs to be importable (don't import the whole agent)
    rows.append(("hermes-agent", "python",
                 importlib.util.find_spec("run_agent") is not None, True))
    for label, exe in _TOOL_HARD:
        rows.append((label, "system", shutil.which(exe) is not None, True))
    for label, exe in _TOOL_SOFT:
        rows.append((label, "system", shutil.which(exe) is not None, False))
    for label, mod in _PY_SOFT:
        rows.append((label, "python", _can_import(mod), False))
    return rows


def doctor(rest=None):
    rows = diagnose()
    print("iva doctor — dependency check\n")
    missing_required = []
    for label, kind, ok, required in rows:
        status = "ok  " if ok else ("FAIL" if required else "warn")
        line = f"  [{status}] {label}  ({kind}, {'required' if required else 'optional'})"
        if not ok:
            line += f"\n          fix: {_FIX.get(label, '')}"
        print(line)
        if required and not ok:
            missing_required.append(label)
    if missing_required:
        print(f"\n{len(missing_required)} required dependency missing: "
              f"{', '.join(missing_required)}")
        return 1
    print("\nAll required dependencies present.")
    return 0


def preflight():
    """Exit with a clear message if any required dependency is missing."""
    missing = [label for label, _kind, ok, required in diagnose() if required and not ok]
    if missing:
        sys.stderr.write(
            "iva: cannot start — missing required dependencies: "
            + ", ".join(missing) + "\n     run `iva doctor` for fixes.\n")
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(doctor(sys.argv[1:]))
