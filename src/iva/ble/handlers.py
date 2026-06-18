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
    # The systemd --user services run with a minimal PATH that excludes
    # ~/.local/bin, where the iva-* helpers live — fall back to it explicitly.
    # wpctl & co. also need XDG_RUNTIME_DIR to find the PipeWire socket.
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    candidates = [cmd]
    local = os.path.expanduser(os.path.join("~", ".local", "bin", cmd[0]))
    if "/" not in cmd[0] and os.path.isfile(local):
        candidates.append([local] + cmd[1:])
    for i, c in enumerate(candidates):
        try:
            p = subprocess.run(c, capture_output=True, text=True,
                               timeout=timeout, env=env)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            if i == len(candidates) - 1:
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
    return {"name": "Iva", "version": getattr(iva, "__version__", "?"),
            "paired": auth.has_paired_devices(), "pair_open": auth.pair_window_open()}


def h_auth_status(_p):
    return {"paired": auth.has_paired_devices(), "pair_open": auth.pair_window_open()}


def h_auth_login(p):
    # Passwordless: a paired-device secret (phones) or a one-time 6-digit
    # code minted by a paired phone (web console).
    try:
        if p.get("code"):
            return {"token": auth.login_code(str(p["code"]))}
        return {"token": auth.login_secret(p.get("secret") or "")}
    except ValueError as e:
        raise CmdError(str(e))


def h_auth_pair(p):
    """Claim or join: open action, but auth.pair itself only succeeds
    out-of-box (no paired devices yet — proximity at setup is the trust) or
    during a pairing window opened from an already-paired phone. Returns the
    durable per-device secret the caller stores and logs in with forever."""
    try:
        return {"secret": auth.pair((p.get("name") or "").strip()[:64])}
    except ValueError as e:
        raise CmdError(str(e))


def h_auth_pair_window(_p):
    """Share access (token-gated): allow ONE more phone to pair in the next
    minute; the BLE broadcast resumes for the duration."""
    return {"open_s": auth.open_pair_window()}


def h_ble_status(_p):
    """Post-pair BLE access (token-gated): is the always-on toggle set, and is
    an on-demand connect window currently open."""
    return {"always": auth.ble_always_on(), "window_open": auth.ble_window_open()}


def h_ble_set_always(p):
    """Keep BLE connectable after pairing (token-gated, persisted). When on, the
    device keeps advertising so a paired phone can always reach it over BLE;
    access stays token-gated. Off restores the quiet-once-online default."""
    return {"always": auth.set_ble_always(bool(p.get("on")))}


def h_ble_open(_p):
    """Open an on-demand BLE connect window (token-gated): the device advertises
    for a few minutes so a paired phone can reconnect over BLE, then goes quiet
    again. Allows NO new pairing — use auth.pair_window to add a phone. Usually
    called over the LAN API, since BLE is dark when this is needed."""
    return {"open_s": auth.open_ble_window()}


def h_auth_web_code(_p):
    """Mint the web console's one-time login code (token-gated). Includes the
    console URL so the app can show 'go here, type this'."""
    code, ttl = auth.web_code()
    info = h_net_info({})
    url = f"{info['scheme']}://{info['ip'] or info['hostname']}:{info['port']}"
    return {"code": code, "ttl_s": ttl, "url": url}


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

# Voice-input mode: "multimodal" = the turn audio goes straight to the LLM
# (one-call 12B audio router, the default), "asr" = transcribe with the STT
# endpoint first + single-call agent. Stored as the IVA_ROUTER env override
# the wake daemon reads (absent/on = multimodal, 0/off = asr); either mode
# keeps STT as the fallback path, so the STT endpoint stays configured.
_VOICE_MODES = ("multimodal", "asr")


def _voice_mode():
    v = (_read_env_override().get("IVA_ROUTER") or "1").strip().lower()
    return "asr" if v in ("0", "false", "no", "off") else "multimodal"


def h_models_get(_p):
    from iva import config_cmd as c
    cfg = c._load()
    out = {k: c._get(cfg, c.KEYMAP[k]) for k in _MODEL_KEYS if not k.endswith(".key")}
    out["voice.mode"] = _voice_mode()
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
        elif k == "voice.mode":
            mode = str(v).strip().lower()
            if mode not in _VOICE_MODES:
                raise CmdError("voice.mode must be 'multimodal' or 'asr'")
            _set_env({"IVA_ROUTER": "0" if mode == "asr" else None})
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


def _profiles_for_ssid(ssid):
    """Saved NetworkManager profiles whose 802-11-wireless.ssid matches."""
    _, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    names = [l.split(":", 1)[0] for l in out.splitlines()
             if l.endswith(":802-11-wireless")]
    matches = []
    for name in names:
        _, s, _ = _run(["nmcli", "-t", "-f", "802-11-wireless.ssid",
                        "connection", "show", name])
        if s.strip().split(":", 1)[-1] == ssid:
            matches.append(name)
    return matches


def h_wifi_connect(p):
    ssid = (p.get("ssid") or "").strip()
    password = p.get("password")
    if not ssid:
        raise CmdError("ssid required")
    # Already on this network → no-op (also avoids nmcli touching the live link).
    if h_wifi_status({}).get("ssid") == ssid:
        return h_wifi_status({})
    # A fresh password should win over any saved profile for this SSID. Stale
    # profiles (e.g. netplan-generated ones without a security section) make
    # `nmcli dev wifi connect` fail with "key-mgmt: property is missing".
    if password:
        for name in _profiles_for_ssid(ssid):
            _run(["nmcli", "connection", "delete", name], timeout=15)
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out, err = _run(cmd, timeout=45)
    if rc != 0:
        msg = (err or out).strip()
        if "key-mgmt" in msg or "Secrets were required" in msg or "802-11-wireless-security" in msg:
            raise CmdError(f"'{ssid}' needs a Wi-Fi password — enter it and try again")
        raise CmdError(msg[:200] or "connect failed")
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


# ------------------------------------------------------------ net (LAN handoff)
def h_net_info(_p):
    """LAN endpoint + TLS pin for the paired app. After BLE onboarding the app
    switches to the faster HTTPS API on the LAN; it verifies the self-signed
    cert by this fingerprint (trust bootstrapped over the BLE channel), and the
    BLE-issued token works there too (shared token store — see ble/auth.py)."""
    import socket
    from iva.api import tls
    tls_on = (os.environ.get("IVA_API_TLS") or "1").strip().lower() not in ("0", "false", "no", "off")
    return {
        "ip": h_wifi_status({}).get("ip"),
        "hostname": socket.gethostname(),
        "port": int(os.environ.get("IVA_API_PORT", "8800")),
        "scheme": "https" if tls_on else "http",
        "cert_sha256": tls.fingerprint() if tls_on else None,
    }


# ------------------------------------------------- integrations (HA + Spotify)
# Assisted sign-up — see iva/integrations.py. All ValueError -> CmdError so the
# app/web get clean user-facing messages.
def _ig():
    from iva import integrations
    return integrations


def h_integrations_status(_p):
    return _ig().status()


def h_integrations_ha_discover(_p):
    return {"instances": _ig().ha_discover()}


def h_integrations_ha_login(p):
    try:
        return _ig().ha_login(p.get("url") or "", p.get("username") or "",
                              p.get("password") or "", p.get("mfa_code"))
    except ValueError as e:
        raise CmdError(str(e))


def h_integrations_spotify_begin(p):
    try:
        return _ig().spotify_begin(p.get("redirect_uri") or "")
    except ValueError as e:
        raise CmdError(str(e))


def h_integrations_spotify_exchange(p):
    try:
        return _ig().spotify_exchange(p.get("code") or "")
    except ValueError as e:
        raise CmdError(str(e))


def h_integrations_spotify_set_device(p):
    try:
        return _ig().spotify_set_device(p.get("name") or "")
    except ValueError as e:
        raise CmdError(str(e))


# Himalaya email CLI (agent operates a mailbox) — assisted setup.
def h_email_status(_p):
    from iva import himalaya
    return himalaya.status()


def h_email_install(_p):
    from iva import himalaya
    try:
        return himalaya.install()
    except Exception as e:
        raise CmdError(str(e)[:300])


def h_email_configure(p):
    from iva import himalaya
    try:
        return himalaya.configure(
            p.get("email") or "", p.get("password") or "",
            imap_host=p.get("imap_host"), imap_port=p.get("imap_port"),
            smtp_host=p.get("smtp_host"), smtp_port=p.get("smtp_port"),
            smtp_encryption=p.get("smtp_encryption"))
    except ValueError as e:
        raise CmdError(str(e))


def h_email_test(_p):
    from iva import himalaya
    try:
        return himalaya.test()
    except ValueError as e:
        raise CmdError(str(e))


# ----------------------------------------------------------------- timezone
def h_device_timezone(_p):
    from iva import config_cmd as c
    return {"tz": (c._load().get("timezone") or "")}


def h_device_set_timezone(p):
    """Set the assistant's IANA timezone (from the paired phone) so its sense
    of time, reminders and cron match the owner. Writes the Hermes `timezone`
    config (what hermes_time.py reads — no sudo); also best-effort sets the
    system clock zone (usually needs sudo, ignored on failure)."""
    tz = (p.get("tz") or "").strip()
    if not tz:
        raise CmdError("tz required")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        raise CmdError(f"unknown timezone {tz!r}")
    from iva import config_cmd as c
    cfg = c._load()
    changed = (cfg.get("timezone") or "") != tz
    c._set(cfg, ("timezone",), tz)
    c._save(cfg)
    # best-effort system tz (no-op without passwordless sudo)
    try:
        subprocess.run(["sudo", "-n", "timedatectl", "set-timezone", tz],
                       capture_output=True, timeout=8)
    except Exception:
        pass
    return {"tz": tz, "changed": changed, "restart_needed": changed}


# -------------------------------------------------------------------- reset
def h_device_reset(_p):
    """Factory-style reset for re-pairing (token-gated): wipes the password,
    paired-device secrets, tokens, TLS cert, app-set settings, history and
    logs, then restarts the services — the BLE setup broadcast comes back on
    and the device onboards like new. Backend endpoints/keys are kept (see
    iva/reset.py). The caller's token dies with the reset, by design."""
    from iva.reset import reset
    return reset(restart=True)


# -------------------------------------------------------------------- apply
def h_apply(_p):
    _run(["systemctl", "--user", "daemon-reload"], timeout=15)  # pick up env drop-in edits
    rc, out, err = _run(["systemctl", "--user", "restart", "hermes-voice.service"], timeout=30)
    if rc != 0:
        raise CmdError((err or out).strip()[:200] or "restart failed")
    return {"restarted": True}


REGISTRY = {
    "hello": h_hello,
    "auth.status": h_auth_status, "auth.login": h_auth_login, "auth.pair": h_auth_pair,
    "auth.pair_window": h_auth_pair_window, "auth.web_code": h_auth_web_code,
    "ble.status": h_ble_status, "ble.set_always": h_ble_set_always, "ble.open": h_ble_open,
    "status.get": h_status_get, "logs.get": h_logs_get,
    "models.get": h_models_get, "models.set": h_models_set,
    "wifi.scan": h_wifi_scan, "wifi.connect": h_wifi_connect, "wifi.status": h_wifi_status,
    "wakeword.list": h_wakeword_list, "wakeword.set": h_wakeword_set, "wakeword.upload": h_wakeword_upload,
    "audio.devices": h_audio_devices, "volume.get": h_volume_get, "volume.set": h_volume_set,
    "bluetooth.scan": h_bt_scan, "bluetooth.pair": h_bt_pair,
    "net.info": h_net_info,
    "integrations.status": h_integrations_status,
    "integrations.ha_discover": h_integrations_ha_discover,
    "integrations.ha_login": h_integrations_ha_login,
    "integrations.spotify_begin": h_integrations_spotify_begin,
    "integrations.spotify_exchange": h_integrations_spotify_exchange,
    "integrations.spotify_set_device": h_integrations_spotify_set_device,
    "email.status": h_email_status, "email.install": h_email_install,
    "email.configure": h_email_configure, "email.test": h_email_test,
    "device.timezone": h_device_timezone, "device.set_timezone": h_device_set_timezone,
    "device.reset": h_device_reset,
    "apply": h_apply,
}
