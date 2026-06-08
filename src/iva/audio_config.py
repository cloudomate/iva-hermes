"""Audio device configuration: named device presets + user overrides.

A *preset* is a standard config for a known device. Built-ins ship with the
package; pick one with ``IVA_AUDIO_PRESET`` (or ``preset:`` in the user file) —
e.g. ``respeaker-xvf3800`` already knows it's a 6-channel array and to extract
channel 0. Users can define their own presets and override any value.

Resolution order (each step overrides the previous):
  1. the ``generic`` built-in (system-default mic/speaker, mono)
  2. the selected preset (built-in, or one you defined)
  3. ``overrides:`` in ~/.config/iva-voice/audio.yaml
  4. explicit env vars: AUDIO_SOURCE / AUDIO_SINK / AUDIO_CHANNELS / WAKE_CH

Fields: source/sink (device name-substring or index; None = system default),
channels (capture channel count; None = auto-try 6 then mono), wake_channel
(which channel to extract when multi-channel), pipewire_profile (for the boot
profile helper, informational).
"""
import os

DEFAULT_PRESET = "generic"

BUILTIN_PRESETS = {
    "generic": {
        "source": None, "sink": None, "channels": None,
        "wake_channel": 0, "pipewire_profile": None,
    },
    # reSpeaker XVF3800: 6ch surround (FL FR FC LFE RL RR); default mono
    # downmixes + dilutes the voice ~5x, so capture 6ch and extract FL (ch 0).
    "respeaker-xvf3800": {
        "source": None, "sink": None, "channels": 6,
        "wake_channel": 0, "pipewire_profile": "analog-surround-51",
    },
}

USER_FILE = os.path.expanduser("~/.config/iva-voice/audio.yaml")


def _load_user():
    if not os.path.isfile(USER_FILE):
        return {}
    try:
        import yaml
        return yaml.safe_load(open(USER_FILE)) or {}
    except Exception as e:
        print(f"[audio] ignoring {USER_FILE}: {e}", flush=True)
        return {}


def available_presets():
    """All known preset names (built-in + user-defined)."""
    user = _load_user()
    return sorted({*BUILTIN_PRESETS, *(user.get("presets") or {})})


def resolve(env=None):
    """Return the effective audio config dict, including the chosen ``preset`` name."""
    env = os.environ if env is None else env
    user = _load_user()
    presets = {**BUILTIN_PRESETS, **(user.get("presets") or {})}

    name = env.get("IVA_AUDIO_PRESET") or user.get("preset") or DEFAULT_PRESET
    cfg = dict(presets.get(DEFAULT_PRESET, {}))
    cfg.update(presets.get(name, {}))                       # selected preset
    cfg.update(user.get("overrides") or {})                 # user overrides

    if env.get("AUDIO_SOURCE"):
        cfg["source"] = env["AUDIO_SOURCE"]
    if env.get("AUDIO_SINK"):
        cfg["sink"] = env["AUDIO_SINK"]
    if env.get("AUDIO_CHANNELS"):
        cfg["channels"] = int(env["AUDIO_CHANNELS"])
    if env.get("WAKE_CH"):
        cfg["wake_channel"] = int(env["WAKE_CH"])

    if cfg.get("channels") is not None:
        cfg["channels"] = int(cfg["channels"])
    cfg["wake_channel"] = int(cfg.get("wake_channel") or 0)
    cfg["preset"] = name
    return cfg
