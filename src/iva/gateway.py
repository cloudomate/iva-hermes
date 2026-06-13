"""Messaging-gateway configuration (Telegram / Discord / Slack / WhatsApp /
Teams / Google Chat / Gmail).

Hermes' gateway bridges the agent to chat platforms. Each platform authenticates
with a handful of env vars in ~/.hermes/.env (the contract Hermes reads), except
WhatsApp which pairs by QR via `hermes whatsapp` (use the Developer terminal).

This module is a DECLARATIVE spec (PLATFORMS) + generic get/set over the env
file, so the UI renders every platform from one description and adding a new one
is just data. Secret values are never returned to the client — only whether each
field is set. Credentials live in ~/.hermes/.env, which a factory reset keeps.

The gateway runs as its own service (`hermes gateway install/start/stop`); we
drive it through the hermes CLI rather than guessing the unit name.
"""
from iva.integrations import _get_env_file, _set_env_file

# field: env var, label, secret? (masked + never returned), optional default,
#        multiline? (textarea), hint
PLATFORMS = [
    {
        "key": "telegram", "name": "Telegram",
        "help": "Create a bot with @BotFather (https://t.me/BotFather) and paste its token.",
        "help_url": "https://t.me/BotFather",
        "fields": [
            {"env": "TELEGRAM_BOT_TOKEN", "label": "Bot token", "secret": True},
            {"env": "TELEGRAM_ALLOWED_USERS", "label": "Allowed user IDs (optional, comma-separated)"},
        ],
    },
    {
        "key": "discord", "name": "Discord",
        "help": "Create an application + bot at the Discord developer portal and paste the bot token.",
        "help_url": "https://discord.com/developers/applications",
        "fields": [
            {"env": "DISCORD_BOT_TOKEN", "label": "Bot token", "secret": True},
        ],
    },
    {
        "key": "slack", "name": "Slack",
        "help": "From your Slack app: Bot token (OAuth & Permissions) + App token (Socket Mode, App-Level Tokens).",
        "help_url": "https://api.slack.com/apps",
        "fields": [
            {"env": "SLACK_BOT_TOKEN", "label": "Bot token (xoxb-…)", "secret": True},
            {"env": "SLACK_APP_TOKEN", "label": "App token (xapp-…)", "secret": True},
        ],
    },
    {
        "key": "whatsapp", "name": "WhatsApp",
        "help": "WhatsApp pairs by scanning a QR code — open the Developer terminal and run the pairing command.",
        "qr": True, "pair_cmd": "hermes whatsapp",
        "fields": [],
    },
    {
        "key": "teams", "name": "Microsoft Teams",
        "help": "Register a bot in the Azure / Bot Framework portal and paste its app credentials.",
        "help_url": "https://dev.botframework.com",
        "fields": [
            {"env": "TEAMS_CLIENT_ID", "label": "Client ID"},
            {"env": "TEAMS_CLIENT_SECRET", "label": "Client secret", "secret": True},
            {"env": "TEAMS_TENANT_ID", "label": "Tenant ID"},
        ],
    },
    {
        "key": "google_chat", "name": "Google Chat (Workspace)",
        "help": "Create a GCP project, enable the Google Chat API + Pub/Sub, and use a service-account JSON.",
        "help_url": "https://console.cloud.google.com/apis/credentials",
        "fields": [
            {"env": "GOOGLE_CHAT_PROJECT_ID", "label": "GCP project ID"},
            {"env": "GOOGLE_CHAT_SUBSCRIPTION_NAME", "label": "Pub/Sub subscription"},
            {"env": "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", "label": "Service-account JSON",
             "secret": True, "multiline": True},
        ],
    },
    {
        "key": "gmail", "name": "Gmail",
        "help": "Enable 2FA, then create an App Password (https://myaccount.google.com/apppasswords).",
        "help_url": "https://myaccount.google.com/apppasswords",
        "fields": [
            {"env": "EMAIL_ADDRESS", "label": "Email address"},
            {"env": "EMAIL_PASSWORD", "label": "App password", "secret": True},
            {"env": "EMAIL_IMAP_HOST", "label": "IMAP host", "default": "imap.gmail.com"},
            {"env": "EMAIL_SMTP_HOST", "label": "SMTP host", "default": "smtp.gmail.com"},
        ],
    },
]

_BY_KEY = {p["key"]: p for p in PLATFORMS}


def _required_envs(plat):
    # required = non-optional, non-defaulted fields (label without "optional")
    return [f["env"] for f in plat["fields"]
            if "optional" not in f["label"].lower() and "default" not in f]


def _platform_state(plat, env):
    fields = []
    for f in plat["fields"]:
        val = env.get(f["env"], "")
        fields.append({
            "env": f["env"], "label": f["label"],
            "secret": bool(f.get("secret")), "multiline": bool(f.get("multiline")),
            "set": bool(val),
            # only non-secret values are echoed back (to prefill/edit)
            "value": "" if f.get("secret") else val,
            "default": f.get("default", ""),
        })
    req = _required_envs(plat)
    configured = bool(req) and all(env.get(e) for e in req)
    if plat.get("qr"):
        configured = (env.get("WHATSAPP_ENABLED", "").lower() in ("1", "true", "yes", "on"))
    return {
        "key": plat["key"], "name": plat["name"], "help": plat["help"],
        "help_url": plat.get("help_url", ""), "qr": bool(plat.get("qr")),
        "pair_cmd": plat.get("pair_cmd", ""),
        "fields": fields, "configured": configured,
    }


def status():
    """Spec + current per-platform state + gateway service status."""
    env = _get_env_file()
    platforms = [_platform_state(p, env) for p in PLATFORMS]
    return {"platforms": platforms, "service": _service_status()}


def set_platform(key, values):
    """Write a platform's credentials into ~/.hermes/.env. values maps env->str;
    empty string clears that var. Returns {configured, restart_needed}."""
    plat = _BY_KEY.get(key)
    if not plat:
        raise ValueError(f"unknown platform {key!r}")
    allowed = {f["env"] for f in plat["fields"]} | (
        {"WHATSAPP_ENABLED"} if plat.get("qr") else set())
    updates = {}
    for k, v in (values or {}).items():
        if k not in allowed:
            raise ValueError(f"unexpected field {k!r}")
        updates[k] = (v or "").strip() or None  # None removes the line
    if updates:
        _set_env_file(updates)
    env = _get_env_file()
    return {**_platform_state(plat, env), "restart_needed": True}


def disconnect(key):
    """Clear all of a platform's env vars (turn it off without re-typing)."""
    plat = _BY_KEY.get(key)
    if not plat:
        raise ValueError(f"unknown platform {key!r}")
    keys = [f["env"] for f in plat["fields"]]
    if plat.get("qr"):
        keys.append("WHATSAPP_ENABLED")
    _set_env_file({k: None for k in keys})
    return {"key": key, "configured": False, "restart_needed": True}


# ----------------------------------------------------------------- service
_GW_ACTIONS = {"status", "start", "stop", "restart", "install", "uninstall"}


def _service_status():
    rc, out = _hermes_gateway("status")
    text = (out or "").strip()
    low = text.lower()
    running = ("not running" not in low and "✗" not in text
               and ("✓" in text or "is running" in low or "active (running)" in low))
    return {"running": running, "detail": text[-400:]}


def service(action):
    if action not in _GW_ACTIONS:
        raise ValueError(f"action must be one of {sorted(_GW_ACTIONS)}")
    rc, out = _hermes_gateway(action, timeout=120)
    return {"action": action, "exit": rc, "output": (out or "").strip()[-4000:],
            "service": _service_status() if action != "status" else None}


def _hermes_gateway(sub, timeout=30):
    from iva.api.actions import _hermes_bin
    from iva.ble.handlers import _run
    # `hermes gateway <sub>` — install adds --accept-hooks so it never blocks
    # on a TTY prompt for unseen shell hooks.
    argv = [_hermes_bin(), "gateway", sub]
    if sub == "install":
        argv.append("--accept-hooks")
    rc, out, err = _run(argv, timeout=timeout)
    return rc, (out or "") + (("\n" + err) if err else "")
