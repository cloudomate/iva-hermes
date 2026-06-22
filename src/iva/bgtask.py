"""Single background task manager for the voice daemon (iva/wake.py).

MVP: ONE long-running agent task at a time. The router (iva/router.py) flags a
turn as `background`; wake.py acks immediately and hands the work here, then
returns to wake-listening. We run the task on a **separate** AIAgent instance
(session `iva-voice-bg`) in a daemon thread, so it never collides with the main
voice agent's in-flight `run_conversation` (the 12B server's 4 slots handle the
concurrent calls). On completion we append a terse record to the SHARED, locked
conversation history and invoke an `on_done(task)` callback — wake.py decides
there whether to voice-announce (user present) or email (away).

Boundaries (documented, not silently capped): one task at a time; no persistence
across daemon restart; cancellation is best-effort (a tool call already running
may still finish). See the plan + CLAUDE.md.
"""
import contextlib
import fcntl
import json
import os
import re
import threading
import time

# Shared with the voice daemon and web console; same resolution as iva/api/chat.py.
HISTORY_FILE = os.environ.get("IVA_HISTORY_FILE",
                              os.path.expanduser("~/.hermes-voice-history.json"))
HISTORY_CAP = int(os.environ.get("IVA_HISTORY_CAP", "20"))

# Strip the mic directives / internal markers a SOUL-equipped agent may emit so
# they never reach TTS or history. (Mirrors wake.strip_directives; duplicated
# here to avoid a circular import with wake.py.)
_DIRECTIVE_RE = re.compile(r"\[+\s*(stay|sleep)\s*\]+", re.IGNORECASE)
_MARKER_RE = re.compile(r"\[+\s*interrupt(?:ed|ing)?\s*\]+", re.IGNORECASE)


def _clean(text):
    text = _DIRECTIVE_RE.sub("", text or "")
    return _MARKER_RE.sub("", text).strip()


# --------------------------------------------------------------- shared history
# A lock FILE (not the data fd) so writers can still publish via atomic os.replace
# while holding it. wake.py uses the same helpers for its own writes, so voice +
# background + web turns serialize on one lock.
@contextlib.contextmanager
def history_lock():
    lf = HISTORY_FILE + ".lock"
    f = open(lf, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _load_unlocked():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def load_history():
    """Lock-held read of the shared history (picks up web + background appends)."""
    with history_lock():
        return _load_unlocked()


def save_history(history):
    """Atomic, lock-held write. Call inside `history_lock()`."""
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, HISTORY_FILE)


def _append_result(desc, result, log):
    """Append a terse completion record so the next foreground turn sees it
    (e.g. user asks 'is it done?'). Capped to HISTORY_CAP."""
    try:
        with history_lock():
            hist = _load_unlocked()
            hist.append({"role": "assistant",
                         "content": "[Background task complete: %s] %s" % (desc, result)})
            save_history(hist[-HISTORY_CAP:])
    except Exception as e:
        log("[bgtask] history append failed: %s" % e)


# ------------------------------------------------------------------ bg agent
_bg_agent = None


def _get_agent():
    """Lazily build the dedicated background agent (separate session from the
    voice agent). Mirrors iva/api/chat.py: reasoning off for gemma-4 via both
    reasoning_effort and the chat-template flag."""
    global _bg_agent
    if _bg_agent is None:
        from hermes_cli.config import load_config
        from run_agent import AIAgent
        m = (load_config().get("model") or {})
        think = (os.environ.get("IVA_THINK") or "").strip().lower() in ("1", "true", "yes", "on")
        overrides = None if think else {
            "reasoning_effort": "none",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        _bg_agent = AIAgent(model=m.get("default"), base_url=m.get("base_url"),
                            api_key=m.get("api_key") or "sk-local",
                            provider=m.get("provider", "custom"), quiet_mode=True,
                            skip_memory=False, session_id="iva-voice-bg",
                            platform="cli", load_soul_identity=True,
                            request_overrides=overrides)
    return _bg_agent


# --------------------------------------------------------------------- state
class _Task:
    __slots__ = ("id", "desc", "status", "started_at", "result", "error",
                 "cancel_event")

    def __init__(self, desc):
        self.id = int(time.time())
        self.desc = desc
        self.status = "running"        # running | done | cancelled | error
        self.started_at = time.time()
        self.result = ""
        self.error = ""
        self.cancel_event = threading.Event()


_current = None
_lock = threading.Lock()


def active():
    with _lock:
        return _current is not None and _current.status == "running"


def current_desc():
    with _lock:
        return _current.desc if _current is not None else None


def started_at():
    with _lock:
        return _current.started_at if _current is not None else None


def status():
    """Snapshot for state.json, or None when idle."""
    with _lock:
        if _current is None:
            return None
        return {"desc": _current.desc, "status": _current.status,
                "since": _current.started_at}


def _worker(task, prompt, history_snapshot, on_done, log):
    try:
        agent = _get_agent()
        agent._interrupt_requested = False
        result = agent.run_conversation(prompt, conversation_history=history_snapshot,
                                        stream_callback=lambda _d: None)
        if task.cancel_event.is_set():
            task.status = "cancelled"
            log("[bgtask] cancelled: %r" % task.desc)
        else:
            reply = (result or {}).get("final_response") or ""
            if not reply:
                msgs = (result or {}).get("messages") or []
                reply = next((m.get("content") for m in reversed(msgs)
                              if m.get("role") == "assistant"
                              and isinstance(m.get("content"), str)), "")
            task.result = _clean(reply)
            task.status = "done"
            log("[bgtask] done: %r (%d chars)" % (task.desc, len(task.result)))
            _append_result(task.desc, task.result, log)
    except Exception as e:
        task.status = "error"
        task.error = str(e)[:200]
        log("[bgtask] error: %s" % task.error)
    finally:
        with _lock:
            globals()["_current"] = None     # free the slot before notifying
        try:
            on_done(task)
        except Exception as e:
            log("[bgtask] on_done error: %s" % e)


def dispatch(desc, prompt, history_snapshot, on_done, log=print):
    """Start a background task. Returns True if accepted, False if one is already
    running (MVP: single task). `on_done(task)` runs on the worker thread when the
    task reaches a terminal state."""
    global _current
    with _lock:
        if _current is not None and _current.status == "running":
            return False
        task = _Task(desc)
        _current = task
    log("[bgtask] dispatch: %r" % desc)
    threading.Thread(target=_worker,
                     args=(task, prompt, list(history_snapshot or []), on_done, log),
                     daemon=True).start()
    return True


def cancel(log=print):
    """Best-effort cancel of the running task. Returns True if one was running.
    Sets the bg agent's interrupt flag (polled between LLM steps); a tool call
    already executing may still complete."""
    with _lock:
        task = _current
    if task is None or task.status != "running":
        return False
    task.cancel_event.set()
    if _bg_agent is not None:
        try:
            _bg_agent._interrupt_requested = True
        except Exception:
            pass
    log("[bgtask] cancel requested: %r" % task.desc)
    return True
