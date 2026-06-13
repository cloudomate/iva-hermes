"""iva-spotify — control Spotify playback for Iva via the Spotify Web API.

A device helper (like iva-volume) the agent calls through the terminal tool.
librespot on rpi3b01 ("Sony HTS") is the always-on Connect speaker → HDMI →
Sony HT-G700; this helper tells Spotify to search + play/pause/skip on it.

Config: ~/.hermes/integrations/spotify.json (off-git, SURVIVES factory reset;
a legacy copy at ~/.config/iva-voice/spotify.json is migrated on first use):
  {"client_id": "...", "refresh_token": "...", "device_name": "Sony HTS"}

Auth is Authorization Code + PKCE (public client — NO client_secret needed):
the companion app / web console drive it via the integrations.* RPC actions
(see iva/integrations.py), so users just log in and approve. The legacy
client_secret flow still works if a secret is present in the config. PKCE
refresh tokens ROTATE — _access_token persists the replacement when Spotify
sends one.

Control:
  iva-spotify play <query>   pause   resume   next   prev   now-playing
  iva-spotify artist|album|playlist <name>   volume <0-100>   louder   quieter
  iva-spotify devices
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CFG = os.path.expanduser(os.environ.get("IVA_SPOTIFY_CONFIG", "~/.hermes/integrations/spotify.json"))
LEGACY_CFG = os.path.expanduser("~/.config/iva-voice/spotify.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
AUTH = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"


def _load():
    try:
        return json.load(open(CFG))
    except Exception:
        pass
    # one-time migration from the pre-reset-survival location
    try:
        cfg = json.load(open(LEGACY_CFG))
        _save(cfg)
        os.unlink(LEGACY_CFG)
        return cfg
    except Exception:
        return {}


def _save(cfg):
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    json.dump(cfg, open(CFG, "w"), indent=2)
    os.chmod(CFG, 0o600)


def _req(url, data=None, method=None, headers=None, form=False):
    if data is not None and form:
        data = urllib.parse.urlencode(data).encode()
    elif data is not None:
        data = json.dumps(data).encode()
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            b = resp.read()
            try:
                return resp.status, (json.loads(b) if b.strip() else {})
            except ValueError:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:300]
        return e.code, {"error": b}


class SpotifyError(Exception):
    """Raisable variant for library callers (the RPC handlers); the CLI maps
    it to die()."""


def get_access_token(cfg):
    """Refresh-token -> access token. PKCE (no secret): client_id goes in the
    body; legacy apps with a client_secret use Basic auth. PKCE rotates the
    refresh token — persist the replacement so the next refresh still works.
    Raises SpotifyError."""
    body = {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cfg.get("client_secret"):
        import base64
        auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
        headers["Authorization"] = "Basic " + auth
    else:
        body["client_id"] = cfg["client_id"]
    st, j = _req(AUTH + "/api/token", data=body, form=True, headers=headers)
    if st != 200 or "access_token" not in j:
        raise SpotifyError(f"token refresh failed ({st}): {j}")
    if j.get("refresh_token") and j["refresh_token"] != cfg["refresh_token"]:
        cfg["refresh_token"] = j["refresh_token"]
        _save(cfg)
    return j["access_token"]


def _access_token(cfg):
    try:
        return get_access_token(cfg)
    except SpotifyError as e:
        die(str(e))


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def H(tok):
    return {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}


def _device_id(tok, name):
    st, j = _req(API + "/me/player/devices", headers=H(tok))
    devs = (j or {}).get("devices", []) if st == 200 else []
    for d in devs:
        if name and name.lower() in d["name"].lower():
            return d["id"], d
    for d in devs:  # else the active one
        if d.get("is_active"):
            return d["id"], d
    return (devs[0]["id"], devs[0]) if devs else (None, None)


def cmd_auth_url(cfg, _):
    p = urllib.parse.urlencode({"client_id": cfg["client_id"], "response_type": "code",
                                "redirect_uri": cfg.get("redirect_uri") or REDIRECT, "scope": SCOPE})
    print(AUTH + "/authorize?" + p)


def cmd_exchange(cfg, args):
    import base64
    raw = args[0] if args else die("usage: exchange <redirect-url-or-code>")
    code = raw
    if "code=" in raw:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [""])[0]
    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    st, j = _req(AUTH + "/api/token",
                 data={"grant_type": "authorization_code", "code": code,
                       "redirect_uri": cfg.get("redirect_uri") or REDIRECT},
                 form=True, headers={"Authorization": "Basic " + auth,
                                     "Content-Type": "application/x-www-form-urlencoded"})
    if st != 200 or "refresh_token" not in j:
        die(f"exchange failed ({st}): {j}")
    cfg["refresh_token"] = j["refresh_token"]
    _save(cfg)
    print("ok — refresh_token saved to", CFG)


def cmd_devices(cfg, _):
    tok = _access_token(cfg)
    st, j = _req(API + "/me/player/devices", headers=H(tok))
    for d in (j or {}).get("devices", []):
        print(("* " if d.get("is_active") else "  ") + f"{d['name']}  ({d['type']}, vol {d.get('volume_percent')})")


def _search(tok, q, typ):
    # NB: limit=1 returns a worse top result than a small batch (Spotify ranking
    # quirk — e.g. "coldplay" gives Ed Sheeran at limit=1, Coldplay at limit=5).
    st, j = _req(API + "/search?" + urllib.parse.urlencode({"q": q, "type": typ, "limit": 5}), headers=H(tok))
    items = (((j or {}).get(typ + "s") or {}).get("items") or []) if st == 200 else []
    return items[0] if items else None


def _play(cfg, tok, body, label):
    dev_id, dev = _device_id(tok, cfg.get("device_name"))
    if not dev_id:
        die("no Spotify device available (the Sony HTS speaker is offline)")
    st, j = _req(API + "/me/player/play?" + urllib.parse.urlencode({"device_id": dev_id}),
                 data=body, method="PUT", headers=H(tok))
    if st not in (200, 202, 204):
        die(f"play failed ({st}): {j}")
    print(f"playing {label} on {dev['name']}")


def cmd_play(cfg, args):
    if not args:
        die("usage: play <query>")
    tok = _access_token(cfg)
    q = " ".join(args)
    tr = _search(tok, q, "track")
    if not tr:
        die(f"no track for {q!r}")
    _play(cfg, tok, {"uris": [tr["uri"]]}, f"{tr['name']} — {tr['artists'][0]['name']}")


def cmd_artist(cfg, args):
    if not args:
        die("usage: artist <name>")
    tok = _access_token(cfg)
    q = " ".join(args)
    a = _search(tok, q, "artist")
    if not a:
        die(f"no artist for {q!r}")
    _play(cfg, tok, {"context_uri": a["uri"]}, f"{a['name']} (top tracks)")


def cmd_album(cfg, args):
    if not args:
        die("usage: album <name>")
    tok = _access_token(cfg)
    q = " ".join(args)
    al = _search(tok, q, "album")
    if not al:
        die(f"no album for {q!r}")
    _play(cfg, tok, {"context_uri": al["uri"]}, f"{al['name']} — {al['artists'][0]['name']}")


def cmd_playlist(cfg, args):
    if not args:
        die("usage: playlist <name>")
    tok = _access_token(cfg)
    q = " ".join(args)
    pl = _search(tok, q, "playlist")
    if not pl:
        die(f"no playlist for {q!r}")
    _play(cfg, tok, {"context_uri": pl["uri"]}, f"{pl['name']} (playlist)")


def _simple(cfg, path, method):
    tok = _access_token(cfg)
    dev_id, _ = _device_id(tok, cfg.get("device_name"))
    url = API + path + (("?" + urllib.parse.urlencode({"device_id": dev_id})) if dev_id and method in ("PUT", "POST") else "")
    st, j = _req(url, method=method, headers=H(tok))
    return st, j


def cmd_pause(cfg, _):
    _simple(cfg, "/me/player/pause", "PUT")
    print("paused")


def cmd_resume(cfg, _):
    _simple(cfg, "/me/player/play", "PUT")
    print("resumed")


def cmd_next(cfg, _):
    _simple(cfg, "/me/player/next", "POST")
    print("next")


def cmd_prev(cfg, _):
    _simple(cfg, "/me/player/previous", "POST")
    print("previous")


def cmd_now(cfg, _):
    tok = _access_token(cfg)
    st, j = _req(API + "/me/player/currently-playing", headers=H(tok))
    it = (j or {}).get("item") if isinstance(j, dict) else None
    if not it:
        print("nothing playing")
        return
    print(f"{it['name']} — {it['artists'][0]['name']}")


def _set_volume(cfg, tok, pct):
    pct = max(0, min(100, int(pct)))
    dev_id, _ = _device_id(tok, cfg.get("device_name"))
    qp = {"volume_percent": pct}
    if dev_id:
        qp["device_id"] = dev_id
    st, j = _req(API + "/me/player/volume?" + urllib.parse.urlencode(qp), method="PUT", headers=H(tok))
    if st not in (200, 202, 204):
        die(f"volume failed ({st}): {j}")
    print(f"volume {pct}%")


def _cur_volume(tok, cfg):
    _, dev = _device_id(tok, cfg.get("device_name"))
    return dev.get("volume_percent", 50) if dev else 50


def cmd_volume(cfg, args):
    if not args:
        die("usage: volume <0-100>")
    _set_volume(cfg, _access_token(cfg), args[0])


def cmd_louder(cfg, _):
    tok = _access_token(cfg)
    _set_volume(cfg, tok, _cur_volume(tok, cfg) + 15)


def cmd_quieter(cfg, _):
    tok = _access_token(cfg)
    _set_volume(cfg, tok, _cur_volume(tok, cfg) - 15)


CMDS = {"auth-url": cmd_auth_url, "exchange": cmd_exchange, "devices": cmd_devices,
        "play": cmd_play, "artist": cmd_artist, "album": cmd_album, "playlist": cmd_playlist,
        "pause": cmd_pause, "resume": cmd_resume, "next": cmd_next, "prev": cmd_prev,
        "now-playing": cmd_now, "volume": cmd_volume, "louder": cmd_louder, "quieter": cmd_quieter}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in CMDS:
        die("usage: iva-spotify {play <q>|artist <q>|album <q>|playlist <q>|pause|resume|next|prev|"
            "now-playing|volume <0-100>|louder|quieter|devices|auth-url|exchange}")
    cfg = _load()
    cmd = args[0]
    if cmd not in ("auth-url", "exchange") and not cfg.get("refresh_token"):
        die("not authorized — connect Spotify from the Iva app (Integrations)")
    CMDS[cmd](cfg, args[1:])


if __name__ == "__main__":
    main()
