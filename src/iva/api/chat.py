"""Text chat through the SAME agent (and the same conversation) as voice.

Mirrors the voice daemon's agent setup (wake.py): same model/base_url/provider
from hermes config, load_soul_identity, memory on, reasoning off by default.
History is the daemon's ~/.hermes-voice-history.json — a web turn and a voice
turn are one continuous conversation. Writes are file-locked + atomic; worst
case under concurrent voice+web turns is a lost turn, never a corrupt file.

Differences from voice: no VOICE_HINT prefix, no TTS, no [[stay]]/[[sleep]]
mic directives (they're stripped from replies for display).
"""
import fcntl
import json
import os
import re
import threading

HISTORY_FILE = os.environ.get("IVA_HISTORY_FILE",
                              os.path.expanduser("~/.hermes-voice-history.json"))
HISTORY_CAP = int(os.environ.get("IVA_HISTORY_CAP", "20"))
_DIRECTIVE_RE = re.compile(r"\[\[(stay|sleep)\]\]", re.I)
# the voice daemon prefixes user turns with a [Voice mode: ...] hint — hide it
_VOICE_HINT_RE = re.compile(r"^\[Voice mode:.*?\]\s*", re.S)

_agent = None
_turn_lock = threading.Lock()  # one in-flight turn (matches the voice loop)


def _get_agent():
    global _agent
    if _agent is None:
        from hermes_cli.config import load_config
        from run_agent import AIAgent
        m = (load_config().get("model") or {})
        think = (os.environ.get("IVA_THINK") or "").strip().lower() in ("1", "true", "yes", "on")
        _agent = AIAgent(model=m.get("default"), base_url=m.get("base_url"),
                         api_key=m.get("api_key") or "sk-local",
                         provider=m.get("provider", "custom"), quiet_mode=True,
                         skip_memory=False, session_id="iva-web-chat", platform="cli",
                         load_soul_identity=True,
                         request_overrides=None if think else {"reasoning_effort": "none"})
    return _agent


def _locked(path, mode, fn):
    with open(path, mode) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            return fn(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_history():
    try:
        return _locked(HISTORY_FILE, "r", lambda f: json.load(f))
    except Exception:
        return []


def save_history(history):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, HISTORY_FILE)


def clean(text):
    return _DIRECTIVE_RE.sub("", text or "").strip()


def display_history(limit=50):
    """Recent user/assistant turns for the chat UI (voice hint + tool noise stripped)."""
    out = []
    for msg in load_history():
        role, content = msg.get("role"), msg.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        if role == "user":
            content = _VOICE_HINT_RE.sub("", content)
        out.append({"role": role, "text": clean(content)})
    return out[-limit:]


def run_turn(text, on_delta=None):
    """Run one text turn through the agent; stream deltas via on_delta(str).
    Returns the final (cleaned) assistant reply."""
    if not (text or "").strip():
        raise ValueError("empty message")
    with _turn_lock:
        agent = _get_agent()
        history = load_history()
        result = agent.run_conversation(
            text, conversation_history=history,
            stream_callback=(lambda d: on_delta(d)) if on_delta else (lambda d: None))
        messages = (result or {}).get("messages") or history
        save_history(messages[-HISTORY_CAP:])
        reply = next((m.get("content") for m in reversed(messages)
                      if m.get("role") == "assistant" and isinstance(m.get("content"), str)), "")
        return clean(reply)
