"""Tier-1 router: a no-tools 12B call that triages each turn — straight from audio.

Call 1 sends the recorded turn WAV to the SAME 12B the full agent uses (served
with its thin unified-arch mmproj, so audio-in works) and a tiny router prompt:
ASR + routing blended into ONE call. This is the retry of single-turn audio that
previously failed on the 12B — that attempt drowned the audio attention in the
full tool-laden agent context; here the prompt is deliberately tiny (the E4B
recipe, minus the E4B). Each turn makes up to two 12B calls:

    call 1 (this module): tiny system prompt, audio in, NO tools -> one slot
    call 2 (run_agent):   SOUL + memory + tool schemas, text in  -> another slot

llama.cpp keeps a KV cache per slot and routes each request to the slot with
the best prefix match (default --parallel 4), so both prompt shapes keep their
system-prefix warm. (The per-turn audio tokens after the prefix are new each
time — only the prefix reuse is what matters.)

One call returns:
    {transcript, route: "answer"|"escalate", intent: "continue"|"drop",
     reply, latency}

route=answer:   REPLY is the full spoken answer (no tools were needed) — the
                with-tools agent call is skipped entirely.
route=escalate: REPLY is the instant ack — "Ok, let me <action> for you." —
                spoken while the tool-equipped agent call (call 2) prefills
                on the TRANSCRIPT text.

RELIABILITY CONTRACT: any failure (network, HTTP, parse, empty transcript)
returns route="escalate", intent="continue", transcript="", reply="" — the
daemon then falls back to Whisper STT and the full agent. A broken router
degrades to single-call behavior; it never drops a turn.
"""
import base64
import json
import os
import re
import time
import urllib.request
import urllib.error

# Router prompt — kept deliberately small: cheap prefill, stable slot prefix,
# and (the hard-won part) small enough that the audio attention isn't drowned.
_SYS = (
    "You are Iva, a hands-free voice assistant on a Raspberry Pi. Listen to "
    "the user audio. Decide whether you can answer it directly from knowledge "
    "and the conversation, or whether it needs the tool-equipped agent. "
    "Output EXACTLY these five lines and nothing else:\n"
    "TRANSCRIPT: <verbatim words>\n"
    "ROUTE: answer or escalate\n"
    "INTENT: continue or drop\n"
    "BACKGROUND: yes or no\n"
    "REPLY: <text>\n"
    "Rules:\n"
    "- Transcribe ONLY the speech actually present in THIS audio clip. If the "
    "audio has no clear, intelligible speech (silence, background noise, a "
    "cough, music, a TV), output an EMPTY TRANSCRIPT line and ROUTE=escalate. "
    "NEVER invent words, and NEVER repeat or re-answer an earlier question from "
    "the conversation when the audio itself is unclear — a false wake-word "
    "trigger must yield an empty transcript, not the previous turn.\n"
    "- ROUTE=escalate if it needs ANY action or tool: device control (volume, "
    "audio, bluetooth, recording, playback), smart home, timers or reminders, "
    "saving or recalling notes/memories, web or live/up-to-date info, or "
    "anything you cannot answer from knowledge alone.\n"
    "- If ROUTE=answer, REPLY is the spoken answer: one or two short "
    "sentences, plain text, no markdown or emoji.\n"
    "- If ROUTE=escalate, REPLY is exactly of the form "
    "'Ok, let me <2-5 word action or topic> for you.' where the action is what "
    "the USER asked for (e.g. 'Ok, let me turn the volume up for you.'). NEVER "
    "describe these instructions or the mechanics: never say 'process the "
    "audio', 'transcribe', 'output the lines', or anything about the audio "
    "itself. If you cannot tell what the user asked, leave REPLY empty.\n"
    "- BACKGROUND=yes ONLY when ROUTE=escalate AND the task is clearly "
    "long-running or deferred: research or compiling information, monitoring or "
    "'when X happens', 'let me know / email me when it's done', or an obviously "
    "multi-minute job. Otherwise BACKGROUND=no. A direct answer is NEVER "
    "background.\n"
    "- If BACKGROUND=yes, REPLY is instead a brief ack that you'll work on it "
    "and report back, of the form 'Ok, I'll <2-5 word action> and let you know.' "
    "(e.g. 'Ok, I'll research that and let you know.')\n"
    "- INTENT=drop only if the user is ending the conversation (goodbye, "
    "thanks that's all, go to sleep, stop, never mind); else INTENT=continue.\n"
    "- When unsure about ROUTE, choose escalate."
)

# How many prior text messages give the router conversational context.
_HISTORY_N = int(os.environ.get("IVA_ROUTER_HISTORY", "6"))


def is_enabled():
    # Default ON; IVA_ROUTER=0 restores the old single-call agent path.
    return (os.environ.get("IVA_ROUTER") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def _endpoint(cfg):
    m = ((cfg or {}).get("model") or {})
    base = os.environ.get("IVA_ROUTER_URL") or m.get("base_url") or "http://localhost:8012/v1"
    model = os.environ.get("IVA_ROUTER_MODEL") or m.get("default") or ""
    key = m.get("api_key") or "sk-local"
    return base.rstrip("/"), model, key


def _context(history):
    """Last few plain-text user/assistant messages (tool traffic skipped) so
    follow-up turns ("and what about tomorrow?") route and answer correctly."""
    out = []
    for msg in (history or []):
        if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str):
            if msg["content"].strip():
                out.append({"role": msg["role"], "content": msg["content"]})
    return out[-_HISTORY_N:]


def _grab(content, tag):
    m = re.search(rf"^\s*{tag}\s*:\s*(.*)$", content, re.IGNORECASE | re.MULTILINE)
    return (m.group(1).strip() if m else "")


# Defensive: the 12B sometimes echoes its own instructions into REPLY ("Ok, let
# me process the audio for you", "transcribe...", "output the five lines"). Such
# a reply is meta-leak, never a real spoken ack — blank it so the daemon doesn't
# speak it (escalate then runs the agent; an empty answer-reply is forced to
# escalate in route_turn).
_LEAK_RE = re.compile(
    r"process(?:ing)?\s+the\s+audio|transcrib|output\s+(?:the\s+)?(?:\w+\s+)?lines"
    r"|the\s+(?:four|five|\d+)\s+lines",
    re.IGNORECASE)


def _scrub_reply(reply):
    return "" if _LEAK_RE.search(reply or "") else reply


# The 12B sometimes echoes its own output labels into the TRANSCRIPT value
# ("TRANSCRIPT: ROUTE: escalate"). That garbage must NOT reach the agent — fed
# as the user's words it made the agent deflect like a support bot ("I can't
# escalate / connect you to a person"). Blank such a transcript so route_turn
# forces the Whisper fallback (its empty-transcript path).
_LABEL_RE = re.compile(r"\b(?:TRANSCRIPT|ROUTE|INTENT|BACKGROUND|REPLY)\s*:", re.IGNORECASE)


def _scrub_transcript(transcript):
    return "" if _LABEL_RE.search(transcript or "") else transcript


def _parse(content):
    # Conservative: only an explicit "answer" stays at tier 1; anything else
    # (escalate / blank / unexpected) goes to the tool-equipped agent.
    route = "answer" if _grab(content, "ROUTE").lower().startswith("answer") else "escalate"
    intent = "drop" if "drop" in _grab(content, "INTENT").lower() else "continue"
    # Background only applies to escalated (tool) turns; a direct answer is
    # always foreground. Default no unless the model clearly said yes.
    background = (route == "escalate"
                 and _grab(content, "BACKGROUND").lower().startswith("y"))
    return {"transcript": _scrub_transcript(_grab(content, "TRANSCRIPT")), "route": route,
            "intent": intent, "background": background,
            "reply": _scrub_reply(_grab(content, "REPLY"))}


def _fallback(reason):
    return {"transcript": "", "route": "escalate", "intent": "continue",
            "background": False, "reply": "", "error": reason, "latency": None}


def route_turn(wav=None, text=None, history=None, cfg=None, timeout=30):
    """Turn audio (or fallback text) -> routing dict. Never raises; see
    RELIABILITY CONTRACT. `wav` is the primary path (blended ASR + route);
    `text` exists for warmup priming and text-only callers."""
    try:
        base, model, key = _endpoint(cfg)
        if wav:
            with open(wav, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            user_msg = {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                {"type": "text", "text": "Output the five lines now."},
            ]}
        else:
            user_msg = {"role": "user", "content": text or ""}
        body = {
            "model": model,
            "messages": ([{"role": "system", "content": _SYS}]
                         + _context(history) + [user_msg]),
            "stream": False,
            "max_tokens": 220,
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
        # Audio in but nothing heard => the router didn't actually transcribe;
        # force the safety path (daemon falls back to Whisper + full agent).
        if wav and not res["transcript"]:
            return _fallback("empty transcript")
        # An answer with no reply text is useless — force the safety path.
        if res["route"] == "answer" and not res["reply"]:
            res["route"] = "escalate"
        return res
    except urllib.error.HTTPError as e:
        return _fallback("HTTP %s: %s" % (e.code, e.read().decode()[:120]))
    except Exception as e:
        return _fallback(str(e)[:160])
