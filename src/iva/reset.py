"""Factory-style reset: return the device to out-of-box onboarding.

Wipes everything a pairing/setup session created — then restarts the
services, which puts the BLE setup broadcast back on the air:

  * ~/.config/iva-voice/        portal password + paired-device secrets,
                                shared tokens, TLS cert (new fingerprint on
                                next start), persisted volume, audio.yaml
  * override.conf               only the Environment= lines (app-set wake
                                word/cutoff/preset); structural directives
                                (ExecStartPre, logging) are deploy-owned
                                and kept
  * conversation history        ~/.hermes-voice-history.json
  * logs                        ~/voicewake.log (truncated)

KEPT on purpose: ~/.hermes/ (backend endpoints + API keys in config.yaml,
long-term memory, and ~/.hermes/integrations/ — the Spotify/Home Assistant
sign-ins) — wiping those leaves the device brainless until someone redoes
OAuth dances the app can't recover silently. Reset is about pairing and
ownership; integrations are configured via the integrations.* RPC actions.

Triggered by `iva reset` (SSH) or the token-gated `device.reset` RPC
(app / web console). The service restart is detached + delayed so an RPC
caller gets its response before its own transport restarts under it.
"""
import os
import shutil
import subprocess

VOICE_DIR = os.path.expanduser("~/.config/iva-voice")
OVERRIDE = os.path.expanduser("~/.config/systemd/user/hermes-voice.service.d/override.conf")
HISTORY = os.path.expanduser("~/.hermes-voice-history.json")
LOG = os.path.expanduser("~/voicewake.log")
UNITS = ["hermes-voice.service", "iva-api.service", "iva-ble.service"]


def reset(restart=True, restart_delay=2):
    removed = []
    if os.path.isdir(VOICE_DIR):
        shutil.rmtree(VOICE_DIR, ignore_errors=True)
        removed.append(VOICE_DIR)
    try:
        with open(OVERRIDE) as f:
            lines = f.read().splitlines()
        kept = [l for l in lines if not l.strip().startswith("Environment=")]
        if len(kept) != len(lines):
            with open(OVERRIDE, "w") as f:
                f.write("\n".join(kept) + "\n")
            removed.append(OVERRIDE + " (Environment= lines)")
    except FileNotFoundError:
        pass
    try:
        os.unlink(HISTORY)
        removed.append(HISTORY)
    except FileNotFoundError:
        pass
    try:
        if os.path.exists(LOG):
            open(LOG, "w").close()
            removed.append(LOG + " (truncated)")
    except Exception:
        pass
    if restart:
        subprocess.Popen(
            ["bash", "-c",
             f"sleep {restart_delay}; systemctl --user daemon-reload; "
             "systemctl --user restart " + " ".join(UNITS)],
            start_new_session=True)
    return {"removed": removed, "restarting": UNITS if restart else []}
