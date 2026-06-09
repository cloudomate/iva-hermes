"""Tier-1 "personal assistant" (PA) front desk for the voice daemon.

Sends the recorded turn WAV to a small, audio-capable LLM (gemma-4-E4B-it served
by llama.cpp with an audio --mmproj) and a tiny router prompt, returning a routing
decision in ONE call:

    {transcript, intent("continue"|"drop"), route("answer"|"escalate"), reply}

The PA owns ASR + the stay/sleep (continue/drop) decision, answers simple turns
itself (route=answer, reply=spoken answer), and for anything needing tools/device
control escalates to the heavy 12B specialist (route=escalate, reply=short ack).

Why this works where one-pass 12B audio did not: E4B is small and we give it a
TINY prompt, so its (encoder-free) audio attention isn't drowned by an
instruction-dense context the way the 12B's was.

RELIABILITY CONTRACT: any failure (network, HTTP, parse, empty transcript) returns
route="escalate", intent="continue" so the turn still reaches the 12B — the daemon
then re-transcribes with the existing STT as a safety net. A broken PA degrades to
today's behavior; it never silently drops a turn.
"""
import base64
import json
import os
import re
import time
import urllib.request
import urllib.error

# Router prompt — kept deliberately tiny (small context = reliable E4B audio).
_SYS = (
    "You are Iva's on-device personal assistant. Listen to the user audio and "
    "output EXACTLY these four lines and nothing else:\n"
    "TRANSCRIPT: <verbatim words>\n"
    "INTENT: continue or drop\n"
    "ROUTE: answer or escalate\n"
    "REPLY: <text>\n"
    "Rules: INTENT=drop if they say goodbye / thanks that's all / go to sleep / "
    "stop / never mind. ROUTE=escalate if they want device control (volume, "
    "audio, recording, playback) or anything needing tools or live/up-to-date "
    "info; otherwise ROUTE=answer. If ROUTE=answer, REPLY is a one-sentence "
    "spoken reply. If ROUTE=escalate, REPLY is a 2-4 word acknowledgement "
    "(e.g. 'On it.'). When unsure, choose escalate."
)


def is_enabled():
    return (os.environ.get("IVA_PA") or "").strip().lower() in ("1", "true", "yes", "on")


def _endpoint(cfg):
    pa = ((cfg or {}).get("personal_assistant") or {})
    base = os.environ.get("IVA_PA_URL") or pa.get("base_url") or "http://gb10dgx01.local:8013/v1"
    model = os.environ.get("IVA_PA_MODEL") or pa.get("model") or "e4b"
    key = pa.get("api_key") or "sk-local"
    return base.rstrip("/"), model, key


def _grab(content, tag):
    m = re.search(rf"^\s*{tag}\s*:\s*(.*)$", content, re.IGNORECASE | re.MULTILINE)
    return (m.group(1).strip() if m else "")


def _parse(content):
    transcript = _grab(content, "TRANSCRIPT")
    intent = "drop" if "drop" in _grab(content, "INTENT").lower() else "continue"
    # Conservative: only an explicit "answer" routes to the PA; anything else
    # (escalate / blank / unexpected) goes to the 12B specialist.
    route = "answer" if _grab(content, "ROUTE").lower().startswith("answer") else "escalate"
    reply = _grab(content, "REPLY")
    return {"transcript": transcript, "intent": intent, "route": route, "reply": reply}


def _fallback(reason):
    return {"transcript": "", "intent": "continue", "route": "escalate",
            "reply": "", "error": reason, "latency": None}


def personal_assistant(wav_path, cfg=None, timeout=30):
    """Audio WAV -> routing dict. Never raises; see RELIABILITY CONTRACT above."""
    try:
        base, model, key = _endpoint(cfg)
        with open(wav_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                    {"type": "text", "text": "Process the audio. Output the four lines now."},
                ]},
            ],
            "stream": False,
            "max_tokens": 160,
            "temperature": 0,
            # gemma4 thinking adds latency + eats the token budget; the router
            # wants a direct structured answer. Verified honored by llama-server.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        t = time.monotonic()
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
        content = (d["choices"][0]["message"].get("content") or "").strip()
        res = _parse(content)
        res["latency"] = round(time.monotonic() - t, 2)
        # Empty transcript = the PA didn't actually hear it; force the safety path.
        if not res["transcript"]:
            res["route"] = "escalate"
            res["intent"] = "continue"
        return res
    except urllib.error.HTTPError as e:
        return _fallback("HTTP %s: %s" % (e.code, e.read().decode()[:120]))
    except Exception as e:
        return _fallback(str(e)[:160])
