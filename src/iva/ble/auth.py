"""Password gate for the setup service.

First boot has no password: the app calls `auth.setup` to set one (stored as a
salted PBKDF2 hash in ~/.config/iva-voice/portal.json, chmod 600). After that,
`auth.login` issues an in-process bearer token that every other action requires.
Tokens are not persisted — a service restart invalidates them (app re-logs in).
"""
import hashlib
import hmac
import json
import os
import secrets
import time

CFG = os.path.expanduser(os.environ.get("IVA_PORTAL_CONFIG", "~/.config/iva-voice/portal.json"))
TTL_S = 12 * 3600
_TOKENS = {}  # token -> monotonic expiry


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


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def is_set():
    return bool(_load().get("pw_hash"))


def setup(password):
    if is_set():
        raise ValueError("password already set")
    if not password or len(password) < 4:
        raise ValueError("password must be at least 4 characters")
    salt = secrets.token_hex(16)
    _save({"salt": salt, "pw_hash": _hash(password, salt)})


def login(password):
    d = _load()
    if not d.get("pw_hash"):
        raise ValueError("no password set yet")
    if not hmac.compare_digest(_hash(password, d["salt"]), d["pw_hash"]):
        raise ValueError("incorrect password")
    tok = secrets.token_urlsafe(24)
    _TOKENS[tok] = time.monotonic() + TTL_S
    return tok


def valid_token(tok):
    exp = _TOKENS.get(tok or "")
    if not exp:
        return False
    if exp <= time.monotonic():
        _TOKENS.pop(tok, None)
        return False
    return True
