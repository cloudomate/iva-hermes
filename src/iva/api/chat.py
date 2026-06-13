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
        # gemma-4 ignores `reasoning_effort: none` but honors the chat-template
        # flag `enable_thinking: false` (same switch the voice router uses) —
        # without it the model spends its whole budget in `reasoning_content`
        # and returns an EMPTY `content` (notably on vision/image turns, where
        # it otherwise runs away to 7k+ reasoning tokens).
        # It MUST go through `extra_body` (the OpenAI SDK drops unknown
        # top-level kwargs); putting our own extra_body here also overrides the
        # transport's `extra_body["reasoning"]={"enabled":True}` for this model.
        overrides = None if think else {
            "reasoning_effort": "none",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        _agent = AIAgent(model=m.get("default"), base_url=m.get("base_url"),
                         api_key=m.get("api_key") or "sk-local",
                         provider=m.get("provider", "custom"), quiet_mode=True,
                         skip_memory=False, session_id="iva-web-chat", platform="cli",
                         load_soul_identity=True,
                         request_overrides=overrides)
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


def _transcribe(b64, fmt):
    """Whisper-transcribe a base64 audio clip (voice note) -> text. Reuses the
    same STT the voice daemon uses; reliable across the agent loop."""
    import base64
    import tempfile
    from tools.transcription_tools import transcribe_audio
    suffix = "." + (fmt or "m4a").lstrip(".")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(base64.b64decode(b64))
        path = f.name
    try:
        r = transcribe_audio(path)
        return (r.get("transcript") or "").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _vision_reply(text, image, image_mime, history):
    """Direct vision call to the (vision-capable) model — bypasses the agent,
    whose own image pipeline (auxiliary.vision) isn't wired for this custom
    provider and fails with a 'technical error'. Mirrors the voice router:
    image BEFORE text, enable_thinking off (else gemma buries the answer in
    reasoning_content and returns empty). No tools — a direct describe/answer.
    Appends the turn to history with a clean text-only user message."""
    import urllib.request
    from hermes_cli.config import load_config
    m = (load_config().get("model") or {})
    base = (m.get("base_url") or "").rstrip("/")
    url = f"data:{image_mime or 'image/jpeg'};base64,{image}"
    content = [{"type": "image_url", "image_url": {"url": url}}]
    if text:
        content.append({"type": "text", "text": text})
    # a little recent text context so follow-ups make sense
    ctx = [{"role": msg["role"], "content": msg["content"]}
           for msg in (history or [])[-6:]
           if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str)]
    body = json.dumps({
        "model": m.get("default"),
        "messages": ctx + [{"role": "user", "content": content}],
        "max_tokens": 400, "temperature": 0.4,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (m.get("api_key") or "sk-local")})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    reply = clean((data["choices"][0]["message"].get("content") or "").strip())
    # persist (clean, no base64)
    hist = load_history()
    hist.append({"role": "user", "content": ((text + " ") if text else "") + "[image]"})
    hist.append({"role": "assistant", "content": reply})
    save_history(hist[-HISTORY_CAP:])
    return reply


def run_turn(text, on_delta=None, image=None, image_mime=None,
             audio=None, audio_format=None):
    """Run one chat turn. Optional attachments:
      image  — base64 image (+ image_mime): a DIRECT vision call (no agent/tools).
      audio  — base64 voice note (+ audio_format): transcribed, then normal turn.
    Returns the final (cleaned) assistant reply."""
    text = (text or "").strip()
    if audio:
        spoken = _transcribe(audio, audio_format)
        if spoken:
            text = (text + "\n" if text else "") + spoken
    if not text and not image:
        raise ValueError("empty message")

    with _turn_lock:
        if image:
            return _vision_reply(text, image, image_mime, load_history())
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
