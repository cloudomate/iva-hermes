"""Assisted sign-up for device integrations (Home Assistant + Spotify).

Non-technical users never create API keys or long-lived tokens by hand:

  * HOME ASSISTANT — the user enters their normal HA username/password in the
    app; we run HA's own login_flow API, exchange the code for a session, then
    mint a LONG-LIVED token over HA's WebSocket (the same thing the HA UI does
    under "Create token") and write HASS_URL/HASS_TOKEN into ~/.hermes/.env —
    the contract the hermes-homeassistant skill already reads. The password is
    used once, in memory, never stored.

  * SPOTIFY — Authorization Code + PKCE with a shared product client_id
    (public client, NO client_secret): the app opens the Spotify consent page,
    the user logs in and approves, and we exchange the returned code using the
    PKCE verifier. The refresh token lands in iva.spotify's config.

Everything lives under ~/.hermes/integrations/ (and ~/.hermes/.env), which a
factory reset (iva/reset.py) deliberately KEEPS — reset is about
pairing/ownership, not about re-doing OAuth dances.

All functions raise ValueError with a user-facing message on failure; the RPC
handlers map that to CmdError.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse

from iva import spotify as sp

ENV_FILE = os.path.expanduser(os.environ.get("IVA_HERMES_ENV", "~/.hermes/.env"))
PKCE_FILE = os.path.expanduser(os.environ.get(
    "IVA_SPOTIFY_PKCE", "~/.hermes/integrations/spotify_pkce.json"))
PKCE_TTL_S = 600

# Product-level Spotify app id (public client; not a secret — PKCE needs no
# client_secret). Per-device override via env or an existing client_id in the
# spotify config.
DEFAULT_SPOTIFY_CLIENT_ID = "c8f4e25aa82b48079f1e07edd560d3a4"

# IndieAuth-style client id HA expects: a URL, with redirect_uri under it.
# localhost works regardless of how the device reaches HA (hass-cli does this).
_HA_CLIENT_ID = "https://localhost/"
_HA_REDIRECT = "https://localhost/?auth_callback=1"


# ----------------------------------------------------------------- env helper
def _set_env_file(updates):
    """Merge KEY=value lines into ~/.hermes/.env, preserving everything else."""
    try:
        with open(ENV_FILE) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in updates:
            seen.add(key)
            out.append(f"{key}={updates[key]}")
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    os.chmod(ENV_FILE, 0o600)


def _get_env_file():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except FileNotFoundError:
        pass
    return env


# -------------------------------------------------------------- home assistant
def ha_discover(timeout=4):
    """Probe the default HA mDNS hostname. Empty list => UI asks for a URL."""
    import requests
    found = []
    for url in ("http://homeassistant.local:8123",):
        try:
            r = requests.get(url + "/api/", timeout=timeout)
            if r.status_code in (200, 401):  # 401 = HA answering, auth required
                found.append({"name": "Home Assistant", "url": url})
        except Exception:
            pass
    return found


def _ha_flow_error(j):
    err = ((j.get("errors") or {}).get("base")) or j.get("error") or "login failed"
    return {"invalid_auth": "wrong username or password",
            "invalid_code": "wrong verification code"}.get(err, str(err))


def ha_login(url, username, password, mfa_code=None, timeout=15):
    """Username/password (+ optional MFA) -> long-lived HASS token in .env.
    Returns {"user", "url", "restart_needed": True} or {"mfa_required": True}."""
    import requests
    url = (url or "").strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    s = requests.Session()
    try:
        # 1. open a login flow
        r = s.post(f"{url}/auth/login_flow", timeout=timeout, json={
            "client_id": _HA_CLIENT_ID, "redirect_uri": _HA_REDIRECT,
            "handler": ["homeassistant", None]})
        flow = r.json()
        if "flow_id" not in flow:
            raise ValueError(f"unexpected reply from {url}: {str(flow)[:120]}")
        # 2. credentials step
        r = s.post(f"{url}/auth/login_flow/{flow['flow_id']}", timeout=timeout,
                   json={"client_id": _HA_CLIENT_ID,
                         "username": username, "password": password})
        step = r.json()
        if step.get("errors"):
            raise ValueError(_ha_flow_error(step))
        # 2b. MFA step (totp) if the account has it
        if step.get("type") != "create_entry":
            if not mfa_code:
                return {"mfa_required": True}
            r = s.post(f"{url}/auth/login_flow/{step['flow_id']}", timeout=timeout,
                       json={"client_id": _HA_CLIENT_ID, "code": mfa_code})
            step = r.json()
            if step.get("type") != "create_entry":
                raise ValueError(_ha_flow_error(step))
        code = step["result"]
        # 3. code -> session tokens
        r = s.post(f"{url}/auth/token", timeout=timeout, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": _HA_CLIENT_ID})
        tokens = r.json()
        access = tokens.get("access_token")
        if not access:
            raise ValueError(f"token exchange failed: {str(tokens)[:120]}")
        # 4. mint a long-lived token over the websocket (what the HA UI does)
        token, user = _ha_mint_long_lived(url, access, timeout)
        # 5. persist the skill contract; revoke the temporary session
        _set_env_file({"HASS_URL": url, "HASS_TOKEN": token})
        try:
            if tokens.get("refresh_token"):
                s.post(f"{url}/auth/token", timeout=5,
                       data={"token": tokens["refresh_token"], "action": "revoke"})
        except Exception:
            pass
        return {"user": user, "url": url, "restart_needed": True}
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"could not reach Home Assistant at {url}: {e}")


def _ha_mint_long_lived(url, access_token, timeout):
    from websockets.sync.client import connect
    ws_url = url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    with connect(ws_url, open_timeout=timeout, close_timeout=5) as ws:
        json.loads(ws.recv(timeout))                       # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        ok = json.loads(ws.recv(timeout))
        if ok.get("type") != "auth_ok":
            raise ValueError("Home Assistant rejected the session")
        ws.send(json.dumps({"id": 1, "type": "auth/long_lived_access_token",
                            "client_name": f"Iva ({int(time.time())})",
                            "lifespan": 3650}))
        res = json.loads(ws.recv(timeout))
        if not res.get("success"):
            raise ValueError(f"could not create token: {res.get('error')}")
        # who are we? (nice for the UI)
        ws.send(json.dumps({"id": 2, "type": "auth/current_user"}))
        user = ""
        try:
            cur = json.loads(ws.recv(timeout))
            user = ((cur.get("result") or {}).get("name")) or ""
        except Exception:
            pass
        return res["result"], user


def _ha_status(timeout=4):
    env = _get_env_file()
    url, token = env.get("HASS_URL"), env.get("HASS_TOKEN")
    out = {"url": url, "token_set": bool(token), "connected": False}
    if url and token:
        try:
            import requests
            r = requests.get(url.rstrip("/") + "/api/", timeout=timeout,
                             headers={"Authorization": "Bearer " + token})
            out["connected"] = r.status_code == 200
        except Exception:
            pass
    return out


# --------------------------------------------------------------------- spotify
def _spotify_client_id():
    cid = (sp._load().get("client_id")
           or os.environ.get("IVA_SPOTIFY_CLIENT_ID")
           or DEFAULT_SPOTIFY_CLIENT_ID)
    if not cid:
        raise ValueError("no Spotify client id configured on this device "
                         "(set IVA_SPOTIFY_CLIENT_ID)")
    return cid


def spotify_begin(redirect_uri):
    """Start PKCE: return the consent URL the app opens. The verifier waits
    on disk (short TTL) for the matching spotify_exchange."""
    cid = _spotify_client_id()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    os.makedirs(os.path.dirname(PKCE_FILE), exist_ok=True)
    with open(PKCE_FILE, "w") as f:
        json.dump({"verifier": verifier, "redirect_uri": redirect_uri,
                   "exp": time.time() + PKCE_TTL_S}, f)
    os.chmod(PKCE_FILE, 0o600)
    auth_url = sp.AUTH + "/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": sp.SCOPE, "code_challenge_method": "S256",
        "code_challenge": challenge})
    return {"auth_url": auth_url}


def spotify_exchange(code):
    """Finish PKCE: code (or full redirect URL) -> refresh token saved into
    the iva-spotify config. Returns {"account": display name}."""
    try:
        with open(PKCE_FILE) as f:
            pkce = json.load(f)
    except Exception:
        raise ValueError("no sign-in in progress — start over")
    if pkce.get("exp", 0) < time.time():
        raise ValueError("sign-in expired — start over")
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
    cid = _spotify_client_id()
    st, j = sp._req(sp.AUTH + "/api/token", form=True,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": pkce["redirect_uri"], "client_id": cid,
                          "code_verifier": pkce["verifier"]})
    if st != 200 or "refresh_token" not in j:
        raise ValueError(f"Spotify sign-in failed ({st}): {str(j)[:120]}")
    cfg = sp._load()
    cfg.pop("client_secret", None)  # PKCE — no secret from here on
    cfg.update({"client_id": cid, "refresh_token": j["refresh_token"]})
    sp._save(cfg)
    try:
        os.unlink(PKCE_FILE)
    except FileNotFoundError:
        pass
    account = ""
    try:
        st, me = sp._req(sp.API + "/me", headers=sp.H(j["access_token"]))
        account = (me or {}).get("display_name") or ""
        if account:
            cfg["account"] = account
            sp._save(cfg)
    except Exception:
        pass
    return {"account": account}


def _spotify_status():
    cfg = sp._load()
    out = {"configured": bool(cfg.get("refresh_token")),
           "device_name": cfg.get("device_name"),
           "account": cfg.get("account") or "", "devices": []}
    if not out["configured"]:
        return out
    try:
        tok = sp.get_access_token(cfg)
        st, j = sp._req(sp.API + "/me/player/devices", headers=sp.H(tok))
        out["devices"] = [{"name": d["name"], "type": d.get("type"),
                           "active": bool(d.get("is_active"))}
                          for d in (j or {}).get("devices", [])]
        if not out["account"]:
            st, me = sp._req(sp.API + "/me", headers=sp.H(tok))
            out["account"] = (me or {}).get("display_name") or ""
    except sp.SpotifyError as e:
        out["error"] = str(e)
    return out


def spotify_set_device(name):
    cfg = sp._load()
    if not cfg.get("refresh_token"):
        raise ValueError("connect Spotify first")
    cfg["device_name"] = (name or "").strip()
    sp._save(cfg)
    return {"device_name": cfg["device_name"]}


# ---------------------------------------------------------------------- status
def status():
    return {"ha": _ha_status(), "spotify": _spotify_status()}
