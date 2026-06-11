"""Command handlers for the setup service.

Each handler takes a params dict and returns a JSON-serializable dict (or raises
CmdError for a clean user-facing failure). They are thin wrappers over the
existing iva config/runtime code plus nmcli (WiFi) and bluetoothctl (speakers),
so the portal reuses one source of truth and the daemon's existing apply/restart.
"""
import base64
import os
import subprocess

from . import auth

# systemd --user env drop-in the wake daemon reads (WAKE_CUTOFF, AUDIO_*, IVA_*, ...)
OVERRIDE = os.path.expanduser("~/.config/systemd/user/hermes-voice.service.d/override.conf")
_UPLOADS = {}  # in-flight chunked wakeword uploads: "name.ext" -> bytearray


class CmdError(Exception):
    """A clean, user-facing failure (returned as {"ok": false, "error": ...})."""


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        raise CmdError(f"{cmd[0]} not found on this device")
    except subprocess.TimeoutExpired:
        raise CmdError(f"{cmd[0]} timed out")


def _read_env_override():
    env = {}
    try:
        with open(OVERRIDE) as f:
            for line in f:
                s = line.strip()
                if s.startswith("Environment="):
                    kv = s[len("Environment="):].strip().strip('"')
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        env[k] = v
    except FileNotFoundError:
        pass
    return env


def _set_env(updates):
    """Merge {KEY: value} into the daemon's env drop-in, PRESERVING every other
    directive (ExecStartPre, StandardOutput, etc.). value None removes the key.
    Takes effect on the next `apply` (daemon-reload + restart)."""
    try:
        with open(OVERRIDE) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = ["[Service]"]
    out, seen = [], set()
    for line in lines:
        s = line.strip()
        if s.startswith("Environment="):
            kv = s[len("Environment="):].strip().strip('"')
            k = kv.split("=", 1)[0]
            if k in updates:
                seen.add(k)
                if updates[k] is None:
                    continue  # remove this key's line
                out.append(f"Environment={k}={updates[k]}")
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen and v is not None:
            out.append(f"Environment={k}={v}")
    if not any(l.strip() == "[Service]" for l in out):
        out.insert(0, "[Service]")
    os.makedirs(os.path.dirname(OVERRIDE), exist_ok=True)
    with open(OVERRIDE, "w") as f:
        f.write("\n".join(out) + "\n")
    return {k: v for k, v in updates.items() if v is not None}


# ----------------------------------------------------------------- auth / hello
def h_hello(_p):
    import iva
    return {"name": "Iva", "version": getattr(iva, "__version__", "?"), "auth_set": auth.is_set()}


def h_auth_status(_p):
    return {"auth_set": auth.is_set()}


def h_auth_setup(p):
    try:
        auth.setup(p.get("password", ""))
        return {"token": auth.login(p["password"])}
    except ValueError as e:
        raise CmdError(str(e))


def h_auth_login(p):
    try:
        return {"token": auth.login(p.get("password", ""))}
    except ValueError as e:
        raise CmdError(str(e))


# ----------------------------------------------------------------------- status
def _read_state():
    import json
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                        "hermes-voice", "state.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"state": "offline"}


def h_status_get(_p):
    # NB: do NOT `import iva.display` — that module starts a rich Live UI at import
    # time (module-level, not under __main__) and would block. Read state directly.
    st = _read_state()
    _, out, _ = _run(["systemctl", "--user", "is-active", "hermes-voice.service"])
    st["service"] = out.strip() or "unknown"
    return st


def h_logs_get(p):
    n = int(p.get("lines", 60))
    _, out, _ = _run(["journalctl", "--user", "-u", "hermes-voice.service",
                      "--no-pager", "-n", str(n)], timeout=15)
    return {"lines": out.splitlines()[-n:]}


# -------------------------------------------------------- models (reuse config_cmd)
_MODEL_KEYS = ["llm.model", "llm.url", "llm.key", "stt.model", "stt.url", "stt.key",
               "tts.model", "tts.voice", "tts.url", "tts.key"]


def h_models_get(_p):
    from iva import config_cmd as c
    cfg = c._load()
    out = {k: c._get(cfg, c.KEYMAP[k]) for k in _MODEL_KEYS if not k.endswith(".key")}
    pa = cfg.get("personal_assistant") or {}
    out["pa.url"] = pa.get("base_url")
    out["pa.model"] = pa.get("model")
    return out


def h_models_set(p):
    from iva import config_cmd as c
    cfg = c._load()
    values = p.get("values") or {}
    if not values:
        raise CmdError("no values given")
    touched_stt = touched_tts = False
    for k, v in values.items():
        if k in c.KEYMAP:
            c._set(cfg, c.KEYMAP[k], v)
            touched_stt |= k.startswith("stt.")
            touched_tts |= k.startswith("tts.")
        elif k == "pa.url":
            c._set(cfg, ("personal_assistant", "base_url"), v)
        elif k == "pa.model":
            c._set(cfg, ("personal_assistant", "model"), v)
        elif k == "pa.key":
            c._set(cfg, ("personal_assistant", "api_key"), v)
        else:
            raise CmdError(f"unknown model key {k!r}")
    if touched_stt:
        c._set(cfg, ("stt", "provider"), "openai")
        c._set(cfg, ("stt", "enabled"), True)
    if touched_tts:
        c._set(cfg, ("tts", "provider"), "openai")
    c._save(cfg)
    return {"saved": True, "restart_needed": True}


# ----------------------------------------------------------------- wifi (nmcli)
def h_wifi_scan(_p):
    _run(["nmcli", "dev", "wifi", "rescan"], timeout=12)
    _, out, _ = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"])
    nets = {}
    for line in out.splitlines():
        parts = line.split(":")
        ssid = parts[0].strip()
        if not ssid:
            continue
        sig = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        secure = bool(parts[2].strip()) if len(parts) > 2 else True
        if ssid not in nets or sig > nets[ssid]["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig, "secure": secure}
    return {"networks": sorted(nets.values(), key=lambda n: -n["signal"])}


def h_wifi_connect(p):
    ssid = (p.get("ssid") or "").strip()
    if not ssid:
        raise CmdError("ssid required")
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if p.get("password"):
        cmd += ["password", p["password"]]
    rc, out, err = _run(cmd, timeout=45)
    if rc != 0:
        raise CmdError((err or out).strip()[:200] or "connect failed")
    return h_wifi_status({})


def h_wifi_status(_p):
    _, gen, _ = _run(["nmcli", "-t", "-f", "STATE,CONNECTIVITY", "general"])
    _, ipo, _ = _run(["hostname", "-I"])
    _, wifi, _ = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    ssid = next((l.split(":", 1)[1] for l in wifi.splitlines() if l.startswith("yes:")), None)
    ip = (ipo.strip().split() or [None])[0]
    return {"general": gen.strip(), "ip": ip, "ssid": ssid}


# -------------------------------------------------------------------- wake word
def _wake_dir():
    return os.environ.get("WAKE_MODELS_DIR") or os.path.expanduser("~/wakewords")


def h_wakeword_list(_p):
    d = _wake_dir()
    models = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json") and os.path.isfile(os.path.join(d, f[:-5] + ".tflite")):
                models.append(f[:-5])
    env = _read_env_override()
    cutoff = env.get("WAKE_CUTOFF") or os.environ.get("WAKE_CUTOFF", "0.97")
    # active = pinned model if set, else the only one if there's just one (else None = all)
    active = env.get("WAKE_ACTIVE") or (models[0] if len(models) == 1 else None)
    return {"dir": d, "models": models, "active": active, "cutoff": float(cutoff)}


def h_wakeword_set(p):
    updates = {}
    if p.get("active"):
        name = str(p["active"])
        if not os.path.isfile(os.path.join(_wake_dir(), name + ".json")):
            raise CmdError(f"no such wake word {name!r}")
        updates["WAKE_ACTIVE"] = name
    if p.get("cutoff") is not None:
        c = float(p["cutoff"])
        if not 0.0 < c <= 1.0:
            raise CmdError("cutoff must be between 0 and 1")
        updates["WAKE_CUTOFF"] = c
    if not updates:
        raise CmdError("nothing to set (give 'active' and/or 'cutoff')")
    _set_env(updates)
    return {"saved": True, "restart_needed": True, "set": updates}


def h_wakeword_upload(p):
    """Receive a wake-model file in chunks. params: name, ext(tflite|json),
    b64 (this chunk), index, total. On the final chunk write into WAKE_MODELS_DIR."""
    name = (p.get("name") or "").strip()
    ext = p.get("ext")
    if ext not in ("tflite", "json") or not name or "/" in name or name.startswith("."):
        raise CmdError("need name + ext (tflite|json), no path separators")
    key = f"{name}.{ext}"
    idx, total = int(p.get("index", 0)), int(p.get("total", 1))
    buf = _UPLOADS.setdefault(key, bytearray())
    buf.extend(base64.b64decode(p.get("b64", "")))
    if idx + 1 < total:
        return {"received": idx + 1, "of": total}
    d = _wake_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, key)
    with open(path, "wb") as f:
        f.write(bytes(buf))
    n = len(buf)
    _UPLOADS.pop(key, None)
    return {"saved": path, "bytes": n, "restart_needed": True}


# --------------------------------------------------------------- audio + volume
def h_audio_devices(_p):
    import sounddevice as sd
    sources, sinks = [], []
    for i, dv in enumerate(sd.query_devices()):
        entry = {"index": i, "name": dv["name"]}
        (sources if dv["max_input_channels"] > 0 else sinks).append(entry)
    return {"sources": sources, "sinks": sinks}


def h_volume_get(_p):
    _, out, _ = _run(["iva-volume", "get"])
    return {"raw": out.strip()}


def h_volume_set(p):
    _, out, err = _run(["iva-volume", "set", str(int(p.get("percent", 100)))])
    return {"raw": (out or err).strip()}


# ----------------------------------------------------- bluetooth (bluetoothctl)
def h_bt_scan(p):
    secs = int(p.get("seconds", 8))
    subprocess.run(["bluetoothctl", "--timeout", str(secs), "scan", "on"],
                   capture_output=True, text=True, timeout=secs + 5)
    _, out, _ = _run(["bluetoothctl", "devices"])
    devs = []
    for l in out.splitlines():
        if l.startswith("Device "):
            parts = l.split(" ", 2)
            devs.append({"mac": parts[1], "name": parts[2] if len(parts) > 2 else parts[1]})
    return {"devices": devs}


def h_bt_pair(p):
    mac = (p.get("mac") or "").strip()
    if not mac:
        raise CmdError("mac required")
    results = {}
    for verb in ("pair", "trust", "connect"):
        rc, out, err = _run(["bluetoothctl", verb, mac], timeout=25)
        results[verb] = rc == 0
    if not results.get("connect"):
        raise CmdError("could not connect to device")
    return {"ok": True, "steps": results}


# -------------------------------------------------------------------- apply
def h_apply(_p):
    _run(["systemctl", "--user", "daemon-reload"], timeout=15)  # pick up env drop-in edits
    rc, out, err = _run(["systemctl", "--user", "restart", "hermes-voice.service"], timeout=30)
    if rc != 0:
        raise CmdError((err or out).strip()[:200] or "restart failed")
    return {"restarted": True}


REGISTRY = {
    "hello": h_hello,
    "auth.status": h_auth_status, "auth.setup": h_auth_setup, "auth.login": h_auth_login,
    "status.get": h_status_get, "logs.get": h_logs_get,
    "models.get": h_models_get, "models.set": h_models_set,
    "wifi.scan": h_wifi_scan, "wifi.connect": h_wifi_connect, "wifi.status": h_wifi_status,
    "wakeword.list": h_wakeword_list, "wakeword.set": h_wakeword_set, "wakeword.upload": h_wakeword_upload,
    "audio.devices": h_audio_devices, "volume.get": h_volume_get, "volume.set": h_volume_set,
    "bluetooth.scan": h_bt_scan, "bluetooth.pair": h_bt_pair,
    "apply": h_apply,
}
