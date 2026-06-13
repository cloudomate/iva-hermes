"""Assisted setup for the Himalaya email CLI (the `himalaya` skill).

DISTINCT from the Gmail messaging-gateway adapter (iva/gateway.py): that lets
people *email the agent*; this lets the agent *operate your mailbox* (read/send)
via the `himalaya` CLI, which needs its own ~/.config/himalaya/config.toml and
the himalaya binary.

We keep users out of TOML/terminal: given an email address + app password (+
IMAP/SMTP host, auto-filled for common providers), we install himalaya if
missing and write a known-good config — including the Gmail folder aliases that
are required or save-to-Sent fails. Password is stored as backend.auth.raw in a
chmod-600 file (fine for a personal device; same trust as EMAIL_PASSWORD in
.env). Config survives factory reset (lives under ~/.config, not wiped... note:
~/.config/iva-voice IS wiped, but ~/.config/himalaya is not).
"""
import os
import shutil
import subprocess

CONFIG = os.path.expanduser(os.environ.get("HIMALAYA_CONFIG", "~/.config/himalaya/config.toml"))

# host -> (imap_host, imap_port, smtp_host, smtp_port, smtp_encryption)
_PROVIDERS = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 587, "start-tls"),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 587, "start-tls"),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com", 587, "start-tls"),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com", 587, "start-tls"),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465, "tls"),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "start-tls"),
}

_GMAIL_ALIASES = """
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "[Gmail]/Sent Mail"
folder.aliases.drafts = "[Gmail]/Drafts"
folder.aliases.trash = "[Gmail]/Trash"
"""


def installed():
    return bool(shutil.which("himalaya")
                or os.path.exists(os.path.expanduser("~/.local/bin/himalaya")))


def status():
    cfg_present = os.path.isfile(CONFIG)
    email = ""
    if cfg_present:
        try:
            with open(CONFIG) as f:
                for line in f:
                    if line.strip().startswith("email"):
                        email = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass
    return {"installed": installed(), "configured": cfg_present, "email": email}


def install():
    """Install the himalaya binary into ~/.local (the upstream install script)."""
    if installed():
        return {"installed": True, "output": "already installed"}
    cmd = ("curl -sSL https://raw.githubusercontent.com/pimalaya/himalaya/master/install.sh "
           "| PREFIX=$HOME/.local sh")
    p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=300)
    if not installed():
        raise ValueError((p.stderr or p.stdout or "install failed").strip()[-400:])
    return {"installed": True, "output": (p.stdout or "").strip()[-600:]}


def _provider(email):
    domain = (email.rsplit("@", 1)[-1] or "").lower()
    return _PROVIDERS.get(domain)


def configure(email, password, imap_host=None, imap_port=None,
              smtp_host=None, smtp_port=None, smtp_encryption=None):
    """Write ~/.config/himalaya/config.toml for one default account."""
    email = (email or "").strip()
    password = password or ""
    if "@" not in email:
        raise ValueError("a valid email address is required")
    if not password:
        raise ValueError("an app password is required")
    prov = _provider(email)
    if prov:
        d_imap_h, d_imap_p, d_smtp_h, d_smtp_p, d_smtp_enc = prov
    else:
        d_imap_h = d_imap_p = d_smtp_h = d_smtp_p = None
        d_smtp_enc = "start-tls"
    imap_host = (imap_host or d_imap_h or "").strip()
    smtp_host = (smtp_host or d_smtp_h or "").strip()
    if not imap_host or not smtp_host:
        raise ValueError("IMAP and SMTP host are required for this provider")
    imap_port = int(imap_port or d_imap_p or 993)
    smtp_port = int(smtp_port or d_smtp_p or 587)
    smtp_enc = (smtp_encryption or d_smtp_enc or "start-tls").strip()
    raw = password.replace('"', '\\"')

    toml = f'''[accounts.default]
email = "{email}"
default = true

backend.type = "imap"
backend.host = "{imap_host}"
backend.port = {imap_port}
backend.encryption.type = "tls"
backend.login = "{email}"
backend.auth.type = "password"
backend.auth.raw = "{raw}"

message.send.backend.type = "smtp"
message.send.backend.host = "{smtp_host}"
message.send.backend.port = {smtp_port}
message.send.backend.encryption.type = "{smtp_enc}"
message.send.backend.login = "{email}"
message.send.backend.auth.type = "password"
message.send.backend.auth.raw = "{raw}"
'''
    if _provider(email) and "gmail" in imap_host:
        toml += _GMAIL_ALIASES

    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        f.write(toml)
    os.chmod(CONFIG, 0o600)
    return {"configured": True, "email": email, "installed": installed()}


def test():
    """Quick connectivity check: list one folder. Returns {ok, output}."""
    if not installed():
        raise ValueError("himalaya is not installed yet")
    bin_ = shutil.which("himalaya") or os.path.expanduser("~/.local/bin/himalaya")
    p = subprocess.run([bin_, "folder", "list"], capture_output=True, text=True, timeout=30)
    return {"ok": p.returncode == 0, "output": ((p.stdout or "") + (p.stderr or "")).strip()[-800:]}
