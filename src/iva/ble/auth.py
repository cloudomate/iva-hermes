"""Passwordless auth for the setup service. Trust model: PROXIMITY AT SETUP.

Out of the box (no paired devices) the BLE broadcast is on and the first
phone to connect simply claims the device with `auth.pair`, receiving a
durable high-entropy device secret (we store only its SHA-256 in
~/.config/iva-voice/portal.json, chmod 600). From then on:

  * that phone logs in silently with its secret (`auth.login {secret}`);
  * the BLE broadcast stops while the device is paired + online;
  * MORE phones join only through a short pairing window (`auth.pair_window`,
    token-gated — opened from an already-paired phone) during which the
    broadcast resumes and ONE `auth.pair` is allowed;
  * the WEB CONSOLE logs in with a short-lived single-use 6-digit code minted
    by a paired phone (`auth.web_code` -> `auth.login {code}`) and gets a
    longer-lived session token — browsers can't hold a BLE pairing secret;
  * recovery is the factory reset (`iva reset` / GPIO button), which clears
    the paired devices and re-opens onboarding.

There is no password anywhere. Tokens are shared across transports: hashes +
expiry in ~/.config/iva-voice/tokens.json (chmod 600), so a BLE login is
valid on the LAN API and vice versa, and sessions survive service restarts.
"""
import hashlib
import json
import os
import secrets
import time

CFG = os.path.expanduser(os.environ.get("IVA_PORTAL_CONFIG", "~/.config/iva-voice/portal.json"))
TOKENS = os.path.expanduser(os.environ.get("IVA_TOKENS_FILE", "~/.config/iva-voice/tokens.json"))
TTL_S = 12 * 3600            # phone session token
WEB_TTL_S = 30 * 24 * 3600   # web-console session (its login is a one-time code)
CODE_TTL_S = 120             # web one-time code lifetime
CODE_MAX_TRIES = 5           # then the code is burned (it's only 6 digits)
PAIR_WINDOW_S = 60           # share-access window (add a NEW phone)
BLE_WINDOW_S = 5 * 60        # on-demand post-pair BLE connect window


def _load():
    try:
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w") as f:
        json.dump(d, f)
    os.chmod(CFG, 0o600)


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def has_paired_devices():
    """True once at least one phone holds a device secret — i.e. someone can
    reach us over the LAN API and the onboarding broadcast can go quiet."""
    return bool(_load().get("devices"))


# ------------------------------------------------------------ pairing window
def open_pair_window():
    """Allow ONE more `auth.pair` for the next PAIR_WINDOW_S seconds (and the
    BLE gate re-advertises while it's open). Token-gated at the handler."""
    d = _load()
    d["pair_window"] = time.time() + PAIR_WINDOW_S
    _save(d)
    return PAIR_WINDOW_S


def pair_window_open():
    return (_load().get("pair_window") or 0) > time.time()


def _close_pair_window(d):
    d.pop("pair_window", None)


# -------------------------------------------------- post-pair BLE access (own)
# After onboarding the broadcast goes quiet (the app reaches us over the LAN
# API). These let a paired OWNER connect over BLE again without a factory reset:
#   * an always-on toggle (persisted, app-settable, or the IVA_BLE_ALWAYS_ON
#     env) — keep advertising so a paired phone can always connect over BLE; and
#   * an on-demand window (open_ble_window) the app opens over the LAN API to
#     make the device advertise for a few minutes, then go quiet again.
# Unlike open_pair_window this allows NO new pairing — access stays token-gated
# (auth.pair self-closes once paired), so it only changes discoverability.
def ble_always_on():
    env = str(os.environ.get("IVA_BLE_ALWAYS_ON", "")).strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    return bool(_load().get("ble_always"))


def set_ble_always(on):
    d = _load()
    d["ble_always"] = bool(on)
    _save(d)
    return bool(on)


def open_ble_window():
    """Advertise on demand for BLE_WINDOW_S so a paired phone can reconnect over
    BLE (token-gated at the handler). Self-closes; allows no new pairing."""
    d = _load()
    d["ble_window"] = time.time() + BLE_WINDOW_S
    _save(d)
    return BLE_WINDOW_S


def ble_window_open():
    return (_load().get("ble_window") or 0) > time.time()


# ------------------------------------------------------------------- pairing
def pair(name=""):
    """Claim (out-of-box) or join (open window): mint a durable per-device
    secret; we store only its hash. One join per window."""
    d = _load()
    if d.get("devices") and not ((d.get("pair_window") or 0) > time.time()):
        raise ValueError("pairing closed — open a pairing window from a paired phone "
                         "(or factory-reset the device)")
    secret = secrets.token_urlsafe(32)
    d.setdefault("devices", {})[_sha(secret)] = {
        "name": name or "device", "created": int(time.time())}
    _close_pair_window(d)
    _save(d)
    return secret


def login_secret(secret):
    """Token for a paired device. The secret is high-entropy random, so a
    plain SHA-256 lookup is sufficient."""
    if _sha(secret or "") not in (_load().get("devices") or {}):
        raise ValueError("unknown device — pair again")
    return _issue_token(TTL_S)


# ----------------------------------------------------------------- web codes
def web_code():
    """Single-use 6-digit code for the web console, minted by a paired phone
    (token-gated at the handler). Short TTL + try limit since it's low entropy."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    d = _load()
    d["web_code"] = {"hash": _sha(code), "exp": time.time() + CODE_TTL_S, "tries": 0}
    _save(d)
    return code, CODE_TTL_S


def login_code(code):
    d = _load()
    wc = d.get("web_code") or {}
    if not wc or wc.get("exp", 0) < time.time() or wc.get("tries", 0) >= CODE_MAX_TRIES:
        d.pop("web_code", None)
        _save(d)
        raise ValueError("code expired — get a fresh one from the Iva app")
    if _sha((code or "").strip()) != wc.get("hash"):
        wc["tries"] = wc.get("tries", 0) + 1
        _save(d)
        raise ValueError("wrong code")
    d.pop("web_code", None)  # single use
    _save(d)
    return _issue_token(WEB_TTL_S)


# -------------------------------------------------------------------- tokens
def _load_tokens():
    try:
        with open(TOKENS) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tokens(d):
    os.makedirs(os.path.dirname(TOKENS), exist_ok=True)
    with open(TOKENS, "w") as f:
        json.dump(d, f)
    os.chmod(TOKENS, 0o600)


def _issue_token(ttl):
    tok = secrets.token_urlsafe(24)
    now = time.time()
    toks = {h: exp for h, exp in _load_tokens().items() if exp > now}  # prune expired
    toks[_sha(tok)] = now + ttl
    _save_tokens(toks)
    return tok


def valid_token(tok):
    if not tok:
        return False
    exp = _load_tokens().get(_sha(tok))
    return bool(exp and exp > time.time())
