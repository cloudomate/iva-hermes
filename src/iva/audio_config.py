"""Audio device configuration: named hardware profiles + user overrides.

A *profile* (a.k.a. preset) is a standard config for a known piece of hardware.
Built-ins ship with the package; plug-and-play hardware is **auto-detected** by
matching the connected sound card's name, and you can still **select** one
manually (from the app/web console, ``IVA_AUDIO_PRESET``, or ``iva run
--preset``) and **configure** its fields.

Profile selection order (first that applies wins):
  1. ``IVA_AUDIO_PRESET`` env (``auto`` = force auto-detect)
  2. ``preset:`` in ~/.config/iva-voice/audio.yaml (``auto`` = auto-detect)
  3. auto-detected from the connected hardware (``match`` substring)
  4. the ``generic`` built-in (system-default mic/speaker, mono)

Field resolution (each step overrides the previous):
  generic defaults -> selected profile -> ``overrides:`` in audio.yaml ->
  explicit env vars (AUDIO_SOURCE/SINK/CHANNELS/WAKE_CH, WAKE_CUTOFF,
  SPEECH_MIN/MULT, IVA_VOL_MAX).

Fields:
  source/sink     device name-substring or index; None = system default
                  (route through PipeWire — do NOT pin a raw ALSA hw device,
                  it skips resampling and fails on rate mismatch).
  channels        capture channel count; None = auto-try 6 then mono.
  wake_channel    which channel to extract when multi-channel.
  pipewire_profile boot card profile (informational, for ensure-xvf helper).
  wake_cutoff     wake sensitivity (lower = more sensitive); None = engine default.
  speech_min/speech_mult  adaptive speech-threshold floor / multiplier; None = default.
  vol_max         volume ceiling for iva-volume (software gain >1.0 amplifies).
  default_volume  volume applied when this profile is freshly selected; None = leave.
  match           sound-card name substring for auto-detect; None = never auto.
  label           human-friendly name for the app.
"""
import os

DEFAULT_PRESET = "generic"

# Every profile is merged onto these, so all keys always exist.
_FIELD_DEFAULTS = {
    "label": "", "match": None,
    "source": None, "sink": None, "channels": None,
    "wake_channel": 0, "pipewire_profile": None,
    "wake_cutoff": None, "speech_min": None, "speech_mult": None,
    "vol_max": None, "default_volume": None,
}

BUILTIN_PRESETS = {
    "generic": {
        "label": "Generic (system default mic/speaker)",
        "source": None, "sink": None, "channels": None,
        "wake_channel": 0, "vol_max": 2.50,
    },
    # reSpeaker XVF3800: 6ch surround (FL FR FC LFE RL RR); default mono
    # downmixes + dilutes the voice ~5x, so capture 6ch and extract FL (ch 0).
    "respeaker-xvf3800": {
        "label": "reSpeaker XVF3800 mic array",
        "match": "XVF3800",
        "source": None, "sink": None, "channels": 6,
        "wake_channel": 0, "pipewire_profile": "analog-surround-51",
        "wake_cutoff": 0.94, "vol_max": 2.50,
    },
    # Anker PowerConf S330: USB speakerphone (mic+speaker), 2ch stereo. Route
    # through PipeWire (source/sink None) so 16 kHz capture resamples — pinning
    # the raw hw device crashes with PaErrorCode -9997. Mono capture; "Okay Iva"
    # peaks ~0.92 so cutoff 0.85; its little speaker distorts above unity gain,
    # so cap vol_max at 1.00.
    "anker-powerconf": {
        "label": "Anker PowerConf S330 speakerphone",
        "match": "PowerConf",
        "source": None, "sink": None, "channels": 1,
        "wake_channel": 0, "wake_cutoff": 0.85,
        "vol_max": 1.00, "default_volume": 1.00,
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


def _save_user(d):
    import yaml
    os.makedirs(os.path.dirname(USER_FILE), exist_ok=True)
    with open(USER_FILE, "w") as f:
        yaml.safe_dump(d, f, default_flow_style=False, sort_keys=False)


def _all_presets(user=None):
    user = _load_user() if user is None else user
    return {**BUILTIN_PRESETS, **(user.get("presets") or {})}


def available_presets():
    """All known profile names (built-in + user-defined)."""
    return sorted(_all_presets())


def _connected_cards():
    """Names of the currently connected sound cards (stdlib only, no PipeWire)."""
    cards = []
    try:
        with open("/proc/asound/cards") as f:
            cards = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    return cards


def detect_preset(presets=None):
    """Auto-detect the profile for the connected hardware. Returns
    (name, matched_card) or (None, None) if nothing matches a profile's
    ``match`` substring."""
    presets = _all_presets() if presets is None else presets
    blob = "\n".join(_connected_cards())
    for name, p in presets.items():
        m = {**_FIELD_DEFAULTS, **p}.get("match")
        if m and m.lower() in blob.lower():
            return name, blob
    return None, None


def _select_name(env, user, presets):
    """Resolve which profile name to use + how it was chosen."""
    want = (env.get("IVA_AUDIO_PRESET") or user.get("preset") or "auto")
    if want and want != "auto":
        if want in presets:
            return want, "selected"
        print(f"[audio] unknown preset {want!r}; falling back to auto", flush=True)
    name, _ = detect_preset(presets)
    if name:
        return name, "detected"
    return DEFAULT_PRESET, "default"


def resolve(env=None):
    """Return the effective audio config dict, including the chosen ``preset``
    name, how it was chosen (``selected_via``), and the auto-detected name."""
    env = os.environ if env is None else env
    user = _load_user()
    presets = _all_presets(user)

    name, via = _select_name(env, user, presets)
    cfg = {**_FIELD_DEFAULTS}
    cfg.update(presets.get(DEFAULT_PRESET, {}))             # generic baseline
    cfg.update(presets.get(name, {}))                       # selected profile
    cfg.update(user.get("overrides") or {})                 # user field overrides

    # explicit env always wins (per-field)
    if env.get("AUDIO_SOURCE"):
        cfg["source"] = env["AUDIO_SOURCE"]
    if env.get("AUDIO_SINK"):
        cfg["sink"] = env["AUDIO_SINK"]
    if env.get("AUDIO_CHANNELS"):
        cfg["channels"] = int(env["AUDIO_CHANNELS"])
    if env.get("WAKE_CH"):
        cfg["wake_channel"] = int(env["WAKE_CH"])
    if env.get("WAKE_CUTOFF"):
        cfg["wake_cutoff"] = float(env["WAKE_CUTOFF"])
    if env.get("SPEECH_MIN"):
        cfg["speech_min"] = float(env["SPEECH_MIN"])
    if env.get("SPEECH_MULT"):
        cfg["speech_mult"] = float(env["SPEECH_MULT"])
    if env.get("IVA_VOL_MAX"):
        cfg["vol_max"] = float(env["IVA_VOL_MAX"])

    if cfg.get("channels") is not None:
        cfg["channels"] = int(cfg["channels"])
    cfg["wake_channel"] = int(cfg.get("wake_channel") or 0)
    cfg["preset"] = name
    cfg["selected_via"] = via
    detected, _ = detect_preset(presets)
    cfg["auto_detected"] = detected
    return cfg


def describe():
    """Profile list for the app: name, label, fields, builtin, active, detected."""
    user = _load_user()
    presets = _all_presets(user)
    active = resolve()
    detected, _ = detect_preset(presets)
    out = []
    for nm in sorted(presets):
        p = {**_FIELD_DEFAULTS, **presets[nm]}
        out.append({
            "name": nm, "label": p.get("label") or nm,
            "builtin": nm in BUILTIN_PRESETS,
            "active": nm == active["preset"],
            "detected": nm == detected,
            "fields": {k: p[k] for k in _FIELD_DEFAULTS},
        })
    return {"profiles": out, "active": active["preset"],
            "selected_via": active["selected_via"], "detected": detected,
            "cards": _connected_cards()}


def select_preset(name):
    """Persist the manual profile choice (``auto`` re-enables auto-detect).
    Effective on the next daemon restart."""
    user = _load_user()
    if name and name != "auto" and name not in _all_presets(user):
        raise ValueError(f"unknown profile {name!r}")
    user["preset"] = name or "auto"
    _save_user(user)
    return user["preset"]


def save_profile(name, fields):
    """Create/update a user profile's fields (persisted to audio.yaml)."""
    if not name or name == "auto":
        raise ValueError("profile name required")
    known = set(_FIELD_DEFAULTS)
    bad = set(fields) - known
    if bad:
        raise ValueError(f"unknown fields: {sorted(bad)}")
    user = _load_user()
    presets = user.setdefault("presets", {})
    base = dict(BUILTIN_PRESETS.get(name) or presets.get(name) or {})
    base.update(fields)
    presets[name] = base
    _save_user(user)
    return {**_FIELD_DEFAULTS, **base, "name": name}
