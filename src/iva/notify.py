"""Proactive notification helpers for background tasks (iva/bgtask.py).

MVP scope: **email** via the himalaya CLI — a self-email to the owner, used as
the context-aware fallback when the user isn't present for a voice announce
(see iva/wake.py presence logic). Messenger push (Telegram/etc) is deferred:
the messaging gateway is inbound-only (people -> agent), so outbound needs new
plumbing or a send-message skill.

Recipient resolution: IVA_OWNER_EMAIL wins, else the address of the configured
himalaya account (parsed from its config.toml — written by iva/himalaya.py).
"""
import os
import re
import shutil
import subprocess

_HIMALAYA_CONFIG = os.path.expanduser(
    os.environ.get("HIMALAYA_CONFIG", "~/.config/himalaya/config.toml"))


def owner_email():
    """Recipient for self-notifications: IVA_OWNER_EMAIL wins; else the email of
    the configured himalaya account (first `email = "..."` in its TOML)."""
    env = (os.environ.get("IVA_OWNER_EMAIL") or "").strip()
    if env:
        return env
    try:
        with open(_HIMALAYA_CONFIG) as f:
            m = re.search(r'^\s*email\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
            return m.group(1) if m else ""
    except Exception:
        return ""


def email_available():
    """True if we could plausibly send an email right now."""
    return bool(shutil.which("himalaya") and owner_email())


def send_email(subject, body, log=print):
    """Self-email via himalaya (raw message on stdin). Returns True on success.
    Never raises — notification must never crash the daemon."""
    to = owner_email()
    if not shutil.which("himalaya"):
        log("[notify] himalaya not installed; cannot email")
        return False
    if not to:
        log("[notify] no owner email (set IVA_OWNER_EMAIL); cannot email")
        return False
    msg = "To: %s\nSubject: %s\n\n%s\n" % (to, subject, body)
    try:
        r = subprocess.run(["himalaya", "message", "send"], input=msg, text=True,
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            log("[notify] emailed %r: %r" % (to, subject))
            return True
        log("[notify] himalaya send failed rc=%s: %s"
            % (r.returncode, (r.stderr or "").strip()[:200]))
        return False
    except Exception as e:
        log("[notify] email error: %s" % e)
        return False
