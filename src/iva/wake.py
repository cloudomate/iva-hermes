#!/usr/bin/env python3
"""Headless wake-word voice assistant for Hermes (iva).
Wake (microWakeWord, FL) -> record -> whisper -> agent -> Kokoro TTS -> play.
After speaking, the LLM's reply directive ([[stay]] / [[sleep]]) decides the next
state: stay opens the mic again with no wake gate (with a 12s idle dropback);
sleep returns to wake-only. Barge-in: user speech during TTS cuts playback and
jumps straight to recording. Multi-turn; publishes state JSON.
"""
import os, sys, json, time, threading, subprocess, glob, re, queue, warnings
# pymicro_wakeword/microwakeword.py:207 raises numpy's "invalid value encountered
# in cast" once per stream open when a NaN slips into its uint8 quantization.
# It's cosmetic (the bad block produces garbage features the model rejects on
# its own) but spams the log. Filter only that specific message so genuine
# RuntimeWarnings elsewhere still surface.
warnings.filterwarnings(
    "ignore",
    message="invalid value encountered in cast",
    category=RuntimeWarning,
)
HERMES = "/home/iva/.hermes/hermes-agent"
sys.path.insert(0, HERMES); os.chdir(HERMES)
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
import numpy as np, sounddevice as sd
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model

RATE = 16000; BLOCK = 1280
# --- audio device selection: a named preset (e.g. respeaker-xvf3800) supplies
# defaults; ~/.config/iva-voice/audio.yaml + env (AUDIO_SOURCE/SINK/CHANNELS/
# WAKE_CH) override. See iva/audio_config.py. ---
from iva.audio_config import resolve as _resolve_audio
_AUDIO = _resolve_audio()
WAKE_CH = _AUDIO["wake_channel"]            # channel read_fl() extracts when multi-channel
AUDIO_SOURCE = _AUDIO["source"]             # device name substring / index / None=default
AUDIO_SINK = _AUDIO["sink"]
AUDIO_CHANNELS = _AUDIO["channels"]         # int, or None = auto (try 6 then mono)
print(f"[audio] preset={_AUDIO['preset']} source={AUDIO_SOURCE!r} sink={AUDIO_SINK!r} "
      f"channels={AUDIO_CHANNELS} wake_ch={WAKE_CH}", flush=True)

def _resolve_dev(spec):
    if spec is None:
        return None
    try:
        return int(spec)            # device index
    except (TypeError, ValueError):
        return spec                 # sounddevice matches a name substring

def _apply_default_devices():
    src, snk = _resolve_dev(AUDIO_SOURCE), _resolve_dev(AUDIO_SINK)
    if src is not None or snk is not None:
        sd.default.device = (src, snk)
_apply_default_devices()
WAKE_CUTOFF = float(os.environ.get("WAKE_CUTOFF", "0.5"))
# Stay-mode safety: if LLM says [[stay]] but user never speaks, drop back to SLEEP.
LISTENING_IDLE_TIMEOUT = float(os.environ.get("LISTENING_IDLE_TIMEOUT", "12.0"))
# Quiet gap after a sleep beep before re-arming wake-listen. Prevents the
# goodbye beep tail / ambient room recovery from triggering a false wake right
# after a session ends.
SLEEP_QUIET_S = float(os.environ.get("SLEEP_QUIET_S", "2.0"))
# Barge-in: how many consecutive over-threshold blocks count as a real
# *interrupt*. 3 * 80ms ~= 240ms of sustained over-threshold speech — fast
# enough for a clear "stop" to land while still debouncing single bursts.
BARGE_HITS_NEEDED = int(os.environ.get("BARGE_HITS_NEEDED", "3"))
# Bar = BARGE_RMS_MULT * max(speech_threshold, calibrated playback leak).
# 2.0x is the looser side of the leak/voice gap: easier to interrupt during
# TTS at the cost of occasional cough/agreement false triggers.
BARGE_RMS_MULT = float(os.environ.get("BARGE_RMS_MULT", "2.0"))
# --- adaptive noise floor: auto-tune speech/silence thresholds to the room ---
noise_floor = [250.0]                                  # EMA of ambient FL RMS
NOISE_ALPHA = 0.05
SPEECH_MULT = float(os.environ.get("SPEECH_MULT", "3.5"))   # speech = floor * this
SPEECH_MIN  = float(os.environ.get("SPEECH_MIN", "600"))    # absolute floor
def speech_threshold():
    return max(SPEECH_MIN, noise_floor[0] * SPEECH_MULT)
def _update_noise(rms):
    # only adapt to quiet samples so speech bursts don't inflate the floor
    if rms < noise_floor[0] * 2.5:
        noise_floor[0] = max(80.0, min(2000.0,
                          (1 - NOISE_ALPHA) * noise_floor[0] + NOISE_ALPHA * rms))
HISTORY_IDLE_RESET = 120.0
STATE_DIR = os.path.join(os.environ["XDG_RUNTIME_DIR"], "hermes-voice")
STATE_FILE = os.path.join(STATE_DIR, "state.json"); os.makedirs(STATE_DIR, exist_ok=True)
_state = {"state":"starting","user":"","reply":"","turns":0,"wakes":0,"model":"","wake_name":"","ts":0.0,"since":0.0}

def publish(state=None, **kw):
    if state is not None: _state["state"]=state; _state["since"]=time.time()
    _state.update(kw); _state["ts"]=time.time()
    try: _state["noise"]=round(noise_floor[0]); _state["thresh"]=round(speech_threshold())
    except Exception: pass
    try:
        tmp=STATE_FILE+".tmp"
        with open(tmp,"w") as f: json.dump(_state,f)
        os.replace(tmp,STATE_FILE)
    except Exception: pass
    # When idling for the wake word, print the wake name so the log reads naturally.
    _st = _state['state']
    _label = (f"listening for '{_state.get('wake_name') or 'wake word'}'"
              if _st == "listening" else _st)
    print(f"[state] {_label}"+(f" user={kw['user']!r}" if 'user' in kw else "")
          +(f" reply={kw['reply'][:60]!r}" if 'reply' in kw else ""), flush=True)

def _tone(freq,dur,vol=0.35):
    t=np.linspace(0,dur,int(RATE*dur),endpoint=False)
    x=np.sin(2*np.pi*freq*t).astype(np.float32)*vol; f=int(RATE*0.008)
    if f*2<x.size: x[:f]*=np.linspace(0,1,f); x[-f:]*=np.linspace(1,0,f)
    return (x*32767).astype(np.int16)
def beep(seq):
    try: sd.play(np.concatenate([_tone(fr,du) for fr,du in seq]),RATE); sd.wait()
    except Exception as e: print("[beep]",e,flush=True)
def beep_wake():  beep([(660,0.10),(990,0.12)])
def beep_sleep(): beep([(660,0.10),(440,0.14)])

publish("loading")
def _default_wake_models_dir():
    """WAKE_MODELS_DIR env wins; else the device dir if present; else the
    wake-word models bundled with this package (greenfield pip install)."""
    env = os.environ.get("WAKE_MODELS_DIR")
    if env:
        return env
    if os.path.isdir("/home/iva/wakewords"):
        return "/home/iva/wakewords"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wakewords")
WAKE_MODELS_DIR = _default_wake_models_dir()
def _set_cutoff(m):
    try: m.probability_cutoff = WAKE_CUTOFF
    except Exception: pass
    return m
def load_wake_models():
    out=[]
    for cfg in sorted(glob.glob(os.path.join(WAKE_MODELS_DIR, "*.json"))):
        try:
            out.append((os.path.splitext(os.path.basename(cfg))[0], _set_cutoff(MicroWakeWord.from_config(cfg))))
            print(f"[init] loaded custom wake model: {cfg}", flush=True)
        except Exception as e:
            print(f"[init] FAILED to load {cfg}: {e}", flush=True)
    if not out:
        out=[("hey_jarvis", _set_cutoff(MicroWakeWord.from_builtin(Model.HEY_JARVIS)))]
        print("[init] no custom models in "+WAKE_MODELS_DIR+"; using bundled hey_jarvis", flush=True)
    return out
wake_models = load_wake_models()
WAKE_LABEL = " / ".join(getattr(mdl, "wake_word", None) or n.replace("_"," ").title()
                        for n, mdl in wake_models)
_state["wake_name"] = WAKE_LABEL
print(f"[init] wake words={[n for n,_ in wake_models]} cutoff={WAKE_CUTOFF} channel=FL({WAKE_CH})", flush=True)
feats = MicroWakeWordFeatures()

from hermes_cli.config import load_config
from run_agent import AIAgent
from tools.voice_mode import create_audio_recorder, play_audio_file, is_whisper_hallucination
from tools.transcription_tools import transcribe_audio
from tools.tts_tool import text_to_speech_tool

cfg=load_config(); m=cfg.get("model",{}); _state["model"]=m.get("default","")
print(f"[init] agent model={m.get('default')} base_url={m.get('base_url')}",flush=True)

# --- TTS direct-call config (bypasses text_to_speech_tool so we can switch
# voice per sentence based on language). Reads the same Kokoro endpoint the
# tool would use, then POSTs /audio/speech directly with a chosen voice. ---
_tts_cfg = (cfg.get("tts", {}) or {}).get("openai", {}) or {}
TTS_BASE_URL = (_tts_cfg.get("base_url") or "http://localhost:8004/v1").rstrip("/")
TTS_API_KEY = _tts_cfg.get("api_key") or "sk-local"
TTS_VOICE_EN = os.environ.get("TTS_VOICE_EN", _tts_cfg.get("voice") or "af_heart")
TTS_VOICE_HI = os.environ.get("TTS_VOICE_HI", "hf_alpha")
TTS_MODEL = _tts_cfg.get("model") or "kokoro"
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
def pick_voice(text):
    return TTS_VOICE_HI if _DEVANAGARI_RE.search(text or "") else TTS_VOICE_EN
import urllib.request as _ureq, urllib.error as _uerr
def synth_speech_http(text, voice):
    """POST to Kokoro's OpenAI-compat /audio/speech; return mp3 file path or None."""
    body = json.dumps({"model": TTS_MODEL, "voice": voice,
                       "input": text, "response_format": "mp3"}).encode("utf-8")
    req = _ureq.Request(f"{TTS_BASE_URL}/audio/speech", data=body,
        headers={"Content-Type":"application/json",
                 "Authorization": f"Bearer {TTS_API_KEY}"})
    try:
        with _ureq.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except _uerr.URLError as e:
        print(f"[tts] http error: {e}", flush=True); return None
    os.makedirs("/tmp/hermes_voice", exist_ok=True)
    path = f"/tmp/hermes_voice/sent_{int(time.time()*1000)}.mp3"
    with open(path, "wb") as f: f.write(data)
    return path
print(f"[init] tts base_url={TTS_BASE_URL} en={TTS_VOICE_EN} hi={TTS_VOICE_HI}", flush=True)
# Voice terseness is injected as a USER-message prefix (not ephemeral_system_prompt,
# which REPLACES the system prompt and disables Hermes\' long-term memory injection).
VOICE_HINT = ("[Voice mode: if the user asks you to DO something on this device "
              "(e.g. change the volume), actually use your tools / run the command "
              "FIRST and verify it succeeded \u2014 never claim an action you did not "
              "perform. Then reply in one or two short spoken sentences, plain text: "
              "no markdown, emoji, or bullet lists.] ")
# Default OFF for reasoning-mode models (gemma4, qwen3, etc.) — their hidden
# <think>/reasoning blocks add 5-50s per turn for marginal voice-quality gain.
# Set env IVA_THINK=1 to re-enable.
#
# IMPORTANT: pass the disable at the OPENAI-COMPAT layer Hermes uses
# (`/v1/chat/completions`), not the native Ollama `/api/chat`. Empirically:
#   - gemma4:12b honors `reasoning_effort: "none"` on /v1 (~1s for trivial prompt)
#   - `think: false` (Ollama native) is ignored on /v1 (~10s, full reasoning emitted)
# So we send the OpenAI-standard `reasoning_effort` at the top level via
# request_overrides — Ollama's /v1 layer translates it correctly per model.
_IVA_THINK = (os.environ.get("IVA_THINK") or "").strip().lower() in ("1","true","yes","on")
_REQ_OVERRIDES = None if _IVA_THINK else {"reasoning_effort": "none"}
print(f"[init] agent think={'ON (model reasons)' if _IVA_THINK else 'OFF (reasoning_effort=none)'}", flush=True)
agent=AIAgent(model=m.get("default"),base_url=m.get("base_url"),api_key=m.get("api_key") or "sk-local",
    provider=m.get("provider","custom"),quiet_mode=True,skip_memory=False,
    session_id="iva-voice-main", platform="cli", load_soul_identity=True,
    request_overrides=_REQ_OVERRIDES)
HISTORY_FILE="/home/iva/.hermes-voice-history.json"
try:
    history=json.load(open(HISTORY_FILE)); print(f"[init] loaded {len(history)} history msgs from disk",flush=True)
except Exception:
    history=[]
last_activity=[0.0]

def _warmup_llm():
    """Prime the LLM KV cache with the REAL turn prefix by running a throwaway
    turn through the SAME path do_turn uses (VOICE_HINT + tools + memory +
    history) — so the cached prefix matches the first real wake and is reused.
    (A bare prompt primes a different, tiny prefix that never gets reused.)
    No persist_user_message, so it doesn't pollute the conversation history."""
    try:
        t0 = time.monotonic()
        agent.run_conversation(VOICE_HINT + "warm up — reply with just ok",
                               conversation_history=history,
                               stream_callback=lambda _d: None)
        print(f"[warmup] primed (real prefix) in {time.monotonic()-t0:.1f}s", flush=True)
        return True
    except Exception as e:
        print(f"[warmup] attempt failed (non-fatal): {e}", flush=True)
        return False

def _wait_for_backend(timeout=None):
    """Wait until the LLM backend (model.base_url) answers. On a Pi boot this
    service auto-starts before the network / the backend host is up, so block
    here (up to IVA_BACKEND_WAIT seconds, default 180) until it's reachable —
    any HTTP response counts. Returns True if reachable."""
    if timeout is None:
        timeout = float(os.environ.get("IVA_BACKEND_WAIT", "180"))
    base = (m.get("base_url") or "").rstrip("/")
    if not base:
        return False
    import urllib.request, urllib.error
    url = base + "/models"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=4)
            print(f"[wait] backend reachable after {time.monotonic()-t0:.0f}s", flush=True)
            return True
        except urllib.error.HTTPError:       # 4xx/5xx = server is up
            print(f"[wait] backend reachable after {time.monotonic()-t0:.0f}s", flush=True)
            return True
        except Exception as e:               # connection refused / DNS / timeout
            print(f"[wait] backend not ready ({type(e).__name__}); retrying...", flush=True)
            time.sleep(4)
    print(f"[wait] backend still unreachable after {timeout:.0f}s; starting anyway", flush=True)
    return False

# Wait for the backend, then warm up, BEFORE serving — so a Pi cold-boot waits
# for the network/backend and the first turn has no cold-start delay.
# IVA_WARMUP=sync (default) | async (background) | off.
_WARMUP = (os.environ.get("IVA_WARMUP") or "sync").strip().lower()
if _WARMUP == "off":
    print("[warmup] disabled (IVA_WARMUP=off)", flush=True)
elif _WARMUP == "async":
    threading.Thread(target=lambda: (_wait_for_backend(), _warmup_llm()), daemon=True).start()
else:
    _wait_for_backend()
    _retries = int(os.environ.get("IVA_WARMUP_RETRIES", "6"))
    print("[warmup] priming before serving (IVA_WARMUP=sync)...", flush=True)
    for _i in range(max(1, _retries)):
        if _warmup_llm():
            break
        time.sleep(3)   # backend may still be starting

def open_input():
    """Open the capture stream. AUDIO_CHANNELS pins the channel count (e.g. 6 for
    the XVF3800); unset = auto (try 6ch then mono). Device follows sd.default
    (AUDIO_SOURCE). read_fl() extracts WAKE_CH only when channels > 1."""
    candidates = (int(AUDIO_CHANNELS),) if AUDIO_CHANNELS else (6, 1)
    last = None
    for ch in candidates:
        try:
            s=sd.RawInputStream(samplerate=RATE,channels=ch,dtype="int16",blocksize=BLOCK); s.start(); print(f"[input] opened {ch}ch",flush=True); return s,ch
        except Exception as e:
            last = e; print(f"[input] {ch}ch failed: {e}",flush=True)
    raise RuntimeError(f"no input stream: {last}")

def read_fl(stream,ch):
    data,_=stream.read(BLOCK)
    if ch==1: return bytes(data)
    arr=np.frombuffer(bytes(data),np.int16).reshape(-1,ch)
    return np.ascontiguousarray(arr[:, min(WAKE_CH,ch-1)]).tobytes()

def wake_listen():
    # Drain ~0.6s after opening: feed audio through the model but IGNORE hits,
    # so the wake/sleep-beep echo (and stale streaming state) clears before we
    # start detecting -> breaks the beep -> false-wake -> beep feedback loop.
    s,ch=open_input(); drain_until=time.monotonic()+0.6
    _dbg = os.environ.get("WAKE_DEBUG"); _peak=0.0; _peakt=time.monotonic()
    try:
        while True:
            fl=read_fl(s,ch); draining=time.monotonic()<drain_until
            _wa=np.frombuffer(fl,np.int16).astype(np.float32)
            if _wa.size: _update_noise(float(np.sqrt(np.mean(_wa*_wa))))
            for f in feats.process_streaming(fl):
                for _name, _m in wake_models:
                    hit=_m.process_streaming(f)
                    if _dbg and _m._probabilities:
                        p=sum(_m._probabilities)/len(_m._probabilities)
                        if p>_peak: _peak=p
                    if hit and not draining:
                        print(f"[wake] matched {_name}", flush=True); return
            if _dbg and (time.monotonic()-_peakt)>=2.0:
                print(f"[wakedbg] peak prob_mean={_peak:.3f} (cutoff={WAKE_CUTOFF})", flush=True)
                _peak=0.0; _peakt=time.monotonic()
    finally:
        try: s.stop(); s.close()
        except Exception: pass

_DIRECTIVE_RE = re.compile(r"\s*\[\[(stay|sleep)\]\]\s*$", re.IGNORECASE)
# Find directives ANYWHERE in text. Weaker models scatter them (start of a
# sentence, middle of a paragraph, repeated, single-bracket variants, etc.).
# Match 1+ opening brackets, the keyword, 1+ closing brackets — catches
# [stay], [[stay]], [[[ sleep ]]], etc.
_ANY_DIRECTIVE_RE = re.compile(r"\[+\s*(stay|sleep)\s*\]+", re.IGNORECASE)
# Defense-in-depth scrub for tokens that look like internal markers leaking
# from the model into the visible response. Catches:
#   - `[interrupted]` and broken-bracket variants ([interrupt, interrupted])
#   - Gemma-4-style `\thought`, `\response`, `\answer`, `\reasoning` tokens
#     that leak even when reasoning_effort=none. Often followed by a newline
#     then real content — we strip just the marker (and its trailing newline),
#     keeping the content after.
_INTERNAL_MARKER_RE = re.compile(
    r"(?:"
        # [interrupted] family — requires at least one bracket so the bare
        # word "interrupt" used naturally in a sentence isn't stripped.
        r"\[+\s*interrupt(?:ed|ing)?\s*\]*"
        r"|\[*\s*interrupt(?:ed|ing)?\s*\]+"
        # Gemma-style backslash markers — `\thought`, `\response`, etc. with
        # optional trailing newline that the model writes before real content.
        r"|\\(?:thought|response|answer|reasoning|reflection|plan)\b\s*\n?"
    r")",
    re.IGNORECASE,
)

def strip_directives(text):
    """Remove every [[stay]]/[[sleep]]/[interrupted] occurrence; return
    (clean_text, last_directive_keyword|None)."""
    if not text: return text, None
    last = None
    for m in _ANY_DIRECTIVE_RE.finditer(text):
        last = m.group(1).lower()
    text = _ANY_DIRECTIVE_RE.sub("", text)
    text = _INTERNAL_MARKER_RE.sub("", text)
    return text, last

# Narration / filler detector lives in a separate module so its vocabulary
# can be tuned and the whole filter can be disabled via env var
# (NARRATION_FILTER=0). See iva/narration_filter.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # fallback for sibling import
from iva.narration_filter import is_narration, is_enabled as _nf_enabled  # noqa: E402
print(f"[init] narration_filter enabled={_nf_enabled()}", flush=True)

# Speaker-verification gate (opt-in via SPEAKER_GATE=1). Embeds the captured
# turn wav with ECAPA-TDNN and compares to a cached enrollment; rejects
# loopback / other-speaker recordings before Whisper. See iva/speaker_gate.py.
from iva.speaker_gate import maybe_build as _maybe_build_gate  # noqa: E402
_speaker_gate = _maybe_build_gate(log=lambda m: print(m, flush=True))

# If the assistant's reply *itself* says goodbye, treat the session as closing
# even if the model forgot to emit [[sleep]] (or emitted [[stay]] anyway —
# weaker models drift like this).
_ASSISTANT_CLOSE_PHRASES = (
    "see you",
    "goodbye",
    "good night",
    "goodnight",
    "have a good",
    "take care",
    "talk to you later",
    "talk soon",
    "off i go",
    "until next time",
    "see you next time",
    "going to sleep",
    "signing off",
    "farewell",
    "catch you later",
)
def assistant_signals_close(text):
    s = (text or "").lower()
    return any(p in s for p in _ASSISTANT_CLOSE_PHRASES)

def parse_directive(reply):
    """Backwards-compat: strip directives and pick the last-seen as next_state.
    Default when absent: 'stay' — matches SOUL.md. The 12s idle timeout is the
    safety net if the user actually wanted to end the conversation."""
    clean, last = strip_directives(reply or "")
    return clean.strip(), (last or "stay")

# Short close-intent utterances we resolve locally — never round-trip the LLM.
# Doubles as a workaround for Hermes' is_whisper_hallucination(), which (rightly)
# treats "Okay, thank you." as a known Whisper hallucination but that string is
# also a perfectly normal way to end a conversation.
_CLOSE_RE = re.compile(
    r"^(ok(ay)?[\s,.!]*)?(thank(s| you)|thx|goodnight|good night|bye|"
    r"that('?s| is) all|we'?re done|stop( listening)?|go to sleep|"
    r"shut up|nevermind|never mind)[\s,.!]*$",
    re.IGNORECASE,
)
def is_close_intent(text):
    return bool(_CLOSE_RE.match((text or "").strip()))

def record_to_silence(max_s=15, silence_s=1.2, start_timeout_s=8, rms_thresh=None):
    if rms_thresh is None: rms_thresh = speech_threshold()
    """Own single-stream recorder on FL (ch0): main thread, one open/close, no
    background AudioRecorder (whose PortAudio teardown segfaulted). Captures
    until ~silence_s of quiet after speech; None if no speech within timeout."""
    import soundfile as sf
    os.makedirs("/tmp/hermes_voice", exist_ok=True)
    s, ch = open_input(); frames=[]; speech=False; t0=time.monotonic(); last=t0
    try:
        while True:
            fl = read_fl(s, ch)
            a = np.frombuffer(fl, np.int16)
            if a.size: frames.append(a.copy())
            rms = float(np.sqrt(np.mean(a.astype(np.float32)**2))) if a.size else 0.0
            now = time.monotonic()
            if rms >= rms_thresh: speech=True; last=now
            if speech and (now-last) >= silence_s: break
            if (not speech) and (now-t0) >= start_timeout_s: return None
            if (now-t0) >= max_s: break
    finally:
        try: s.stop(); s.close()
        except Exception: pass
    if not speech or not frames: return None
    path = f"/tmp/hermes_voice/turn_{int(t0)}.wav"
    sf.write(path, np.concatenate(frames), RATE, subtype="PCM_16")
    return path

# How many blocks (~80ms each) to calibrate the TTS-loopback leak floor before
# allowing barge-in to fire. Prevents the leakage onset from being mistaken for
# user speech. ~8 blocks = ~640ms.
BARGE_CALIBRATE_BLOCKS = int(os.environ.get("BARGE_CALIBRATE_BLOCKS", "8"))

def _barge_in_monitor(interrupted, stop_evt, playing):
    """Watch FL for user speech during TTS. Sets `interrupted` on hit.

    The XVF3800's AEC doesn't fully suppress Iva's own voice on FL — playback
    leakage at the mic can reach several thousand RMS. A fixed threshold can't
    separate leakage from real user voice, so we track an EMA of RMS *while
    audio is playing* (leak_floor) and require a clear excursion above it to
    fire. While not playing, fall back to the normal speech_threshold gate."""
    try:
        s, ch = open_input()
    except Exception as e:
        print("[barge] cannot open input:", e, flush=True); return
    try:
        leak_floor = 0.0; leak_blocks = 0; hits = 0
        was_playing = False
        while not stop_evt.is_set():
            a = np.frombuffer(read_fl(s, ch), np.int16).astype(np.float32)
            if not a.size: continue
            rms = float(np.sqrt(np.mean(a*a)))
            is_playing = playing.is_set()
            if is_playing and not was_playing:
                leak_floor = 0.0; leak_blocks = 0  # reset calibration on each playback start
            was_playing = is_playing

            if not is_playing:
                # Don't fire while no TTS is on the wire — there's no audio to
                # barge-in over yet (LLM may still be thinking). Eliminates
                # false triggers from ambient noise / wake-beep tail.
                hits = 0
                continue
            # Calibrate to the leak level while playing. EMA so it tracks
            # varying TTS loudness across sentences.
            leak_floor = rms if leak_blocks == 0 else (0.9*leak_floor + 0.1*rms)
            leak_blocks += 1
            if leak_blocks <= BARGE_CALIBRATE_BLOCKS:
                hits = 0  # don't fire during calibration
                continue
            bar = max(speech_threshold() * BARGE_RMS_MULT, leak_floor * BARGE_RMS_MULT)

            if rms >= bar:
                hits += 1
                if hits >= BARGE_HITS_NEEDED:
                    print(f"[barge] interrupt rms={rms:.0f} bar={bar:.0f} leak={leak_floor:.0f} playing={is_playing}", flush=True)
                    interrupted.set(); return
            else:
                hits = 0
    finally:
        try: s.stop(); s.close()
        except Exception: pass

# Sentence segmenter for streaming TTS. Requires a letter or closing bracket
# before the punctuation so numbers ("1.5") and abbreviations don't false-split.
_SENT_RE = re.compile(r"(?<=[a-zA-Z\)\]\"'`])[.!?](?:\s+|$)")

def _synth_play_worker(text_q, interrupted, playing):
    """Pop sentences off text_q, synth via Hermes Kokoro TTS, play sequentially.
    Drains the queue (no-op) once `interrupted` is set so we drop pending audio
    fast on barge-in. None on the queue is the done sentinel. `playing` is set
    for the duration of each sd.play so the barge-in monitor knows when TTS
    audio is live (for loopback-leak calibration)."""
    import soundfile as _sf
    while True:
        item = text_q.get()
        if item is None: return
        if interrupted.is_set(): continue
        try:
            t0 = time.monotonic()
            voice = pick_voice(item)
            mp3 = synth_speech_http(item, voice)
            if not mp3:
                print("[tts] failed: no audio returned", flush=True); continue
            wav = mp3.rsplit(".", 1)[0] + ".play.wav"
            subprocess.run(["ffmpeg","-y","-loglevel","error","-i",mp3,"-ar","24000","-ac","1",wav], check=False)
            data, sr = _sf.read(wav, dtype="int16")
            print(f"[synth] tts={time.monotonic()-t0:.2f}s audio_s={len(data)/sr:.2f} voice={voice} text={item[:60]!r}", flush=True)
            sd.play(data, sr); playing.set()
            try:
                while True:
                    if interrupted.is_set(): sd.stop(); break
                    try: stream = sd.get_stream()
                    except Exception: stream = None
                    if stream is None or not stream.active: break
                    time.sleep(0.04)
            finally:
                playing.clear()
        except Exception as e:
            print("[tts] error:", e, flush=True); playing.clear()

def _persist_history():
    try: json.dump(history, open(HISTORY_FILE,"w"))
    except Exception as _e: print("[hist] save failed:",_e,flush=True)

def _mark_last_assistant_interrupted():
    """Previously appended ' [interrupted]' to the most recent assistant
    message — but smaller models parroted that literal token back into their
    next reply ("[interrupt" reaching TTS). Now a no-op: the model gets the
    interrupting utterance as the next user turn, which is signal enough."""
    return

def do_turn(start_timeout_s=8):
    """Run one capture -> STT -> LLM -> TTS turn.
    Returns next_state: 'sleep' | 'stay'. 'stay' means keep the mic open with
    no wake gate; 'sleep' returns to wake-only."""
    global history
    publish("recording")
    t_rec0 = time.monotonic()
    wav = record_to_silence(start_timeout_s=start_timeout_s)
    if not wav:
        # No speech captured within timeout (idle dropback or no wake follow-up).
        return "sleep"
    # Speaker-verification gate: reject loopback / other-speaker captures BEFORE
    # spending a Whisper call. Self-disables if SPEAKER_GATE != 1 or enrollment
    # missing — falls through to normal Whisper path.
    if _speaker_gate is not None:
        try:
            t_gate0 = time.monotonic()
            accepted, cos = _speaker_gate.passes(wav)
            print(f"[gate] cos={cos:+.3f} t={time.monotonic()-t_gate0:.2f}s accepted={accepted}", flush=True)
            if not accepted:
                print(f"[turn] not-target-speaker (cos={cos:+.3f}), skipping STT/LLM", flush=True)
                return "sleep"
        except Exception as _e:
            print(f"[gate] error, falling through: {_e}", flush=True)
    t_stt0 = time.monotonic()
    publish("transcribing"); r=transcribe_audio(wav); text=(r.get("transcript") or "").strip()
    print(f"[t] rec={t_stt0-t_rec0:.2f}s stt={time.monotonic()-t_stt0:.2f}s text={text!r} wav={wav}", flush=True)
    if is_close_intent(text):
        # User explicitly closed (or Whisper hallucinated a close-sounding phrase
        # from silence — same effect either way). Skip the LLM entirely; the
        # sleep beep in the main loop's finally is the acknowledgement.
        print(f"[turn] close intent ({text!r}) -> sleep", flush=True)
        return "sleep"
    if not text or is_whisper_hallucination(text):
        print(f"[turn] no usable speech ({text!r})",flush=True); return "sleep"
    publish("thinking", user=text)
    # Clear any stale interrupt flag from a previous turn — otherwise the
    # LLM call would abort instantly on a barge-in we already serviced.
    agent._interrupt_requested = False
    # --- streaming pipeline state ---
    interrupted = threading.Event(); stop_mon = threading.Event()
    playing = threading.Event()   # set while TTS audio is on the wire
    text_q = queue.Queue()
    buf = [""]; spoken_idx = [0]   # mutable boxes for the nested callback
    seen_dir = [None]              # last directive seen in stream (anywhere)
    recent_sentences = []          # for dedupe: weaker models often repeat
    content_enqueued = [False]     # have we shipped a non-narration sentence yet?
    MAX_SENTENCES_PER_TURN = int(os.environ.get("MAX_SENTENCES_PER_TURN", "4"))
    t_thinking = time.monotonic()
    first_delta_t = [0.0]; n_sentences = [0]

    def _cancel_llm(reason):
        """Tell Hermes to abort the in-flight LLM stream. AIAgent polls
        `_interrupt_requested` during streaming chat completions, so flipping
        it raises out of the run_conversation call within ~300ms."""
        if not getattr(agent, "_interrupt_requested", False):
            print(f"[stream] cancel-llm: {reason}", flush=True)
        agent._interrupt_requested = True

    def stream_cb(delta):
        if interrupted.is_set():
            _cancel_llm("barge-in fired")
            return
        if not delta: return
        if first_delta_t[0] == 0.0:
            first_delta_t[0] = time.monotonic()
            print(f"[stream] first delta at {first_delta_t[0]-t_thinking:.2f}s len={len(delta)}", flush=True)
        # Note: don't strip directives at delta granularity — `[[stay]]` often
        # arrives split across deltas (`[[`, `stay`, `]]`). Strip on the
        # *complete sentence chunk* below instead.
        buf[0] += delta
        s = spoken_idx[0]
        while True:
            m = _SENT_RE.search(buf[0], s)
            if not m: break
            raw = buf[0][s:m.end()]
            clean, dirv = strip_directives(raw)
            if dirv: seen_dir[0] = dirv
            sentence = clean.strip()
            if not sentence:
                s = m.end(); continue
            if sentence in recent_sentences:
                print(f"[stream] skip-dup t={time.monotonic()-t_thinking:.2f}s {sentence!r}", flush=True)
                s = m.end(); continue
            if content_enqueued[0] and is_narration(sentence):
                print(f"[stream] skip-narr t={time.monotonic()-t_thinking:.2f}s {sentence!r}", flush=True)
                s = m.end(); continue
            if n_sentences[0] >= MAX_SENTENCES_PER_TURN:
                print(f"[stream] skip-cap t={time.monotonic()-t_thinking:.2f}s (max={MAX_SENTENCES_PER_TURN}) {sentence!r}", flush=True)
                _cancel_llm("sentence cap reached")
                s = m.end(); continue
            recent_sentences.append(sentence)
            if len(recent_sentences) > 6: recent_sentences.pop(0)
            n_sentences[0] += 1
            print(f"[stream] enqueue#{n_sentences[0]} t={time.monotonic()-t_thinking:.2f}s {sentence!r}", flush=True)
            if not content_enqueued[0]:
                # First real spoken sentence — promote state from "thinking"
                # to "speaking" now (not at LLM start). User/UI sees thinking
                # for the entire tool-call window, then speaking when audio is
                # actually about to play.
                publish("speaking", user=text, reply=sentence[:60])
            text_q.put(sentence)
            content_enqueued[0] = True
            s = m.end()
        spoken_idx[0] = s

    mon = threading.Thread(target=_barge_in_monitor, args=(interrupted, stop_mon, playing), daemon=True)
    synth = threading.Thread(target=_synth_play_worker, args=(text_q, interrupted, playing), daemon=True)
    # NOTE: state stays "thinking" while the LLM is running (including
    # tool-call iterations like ha_call_service). We flip to "speaking" only
    # when first audio is actually about to be queued — see stream_cb above.
    # That way the display/log accurately reflects whether the model is still
    # working vs. about to speak.
    t_start = time.monotonic()
    mon.start(); synth.start()

    reply = ""
    try:
        result = agent.run_conversation(
            VOICE_HINT + text,
            conversation_history=history,
            persist_user_message=text,
            stream_callback=stream_cb,
        )
        reply = (result.get("final_response") or "").strip()
        if result.get("messages"):
            # Keep history short — every prior message gets re-evaluated by
            # the LLM on each turn; 60 messages was making prompt prefill ~10s.
            # Env-tunable: IVA_HISTORY_CAP (default 20 = ~3x faster turns).
            history = result["messages"][-int(os.environ.get("IVA_HISTORY_CAP", "20")):]; _persist_history()
    except Exception: import traceback; traceback.print_exc()
    _state["turns"] += 1

    # Reconcile streamed buffer vs LLM's final_response; trust the longer one.
    # buf[0] already has directives stripped (done in stream_cb). reply
    # (Hermes' final_response) may still contain them — scrub once more for safety.
    final_buf = buf[0] if len(buf[0]) >= len(reply) else reply
    clean_full, last_in_buf = strip_directives(final_buf)
    clean_full = (clean_full or "").strip()
    # Prefer what we caught during streaming; fall back to a final scan; then
    # to the "stay" default (matches SOUL.md and the conversational expectation).
    directive = seen_dir[0] or last_in_buf or "stay"
    _had_directive = bool(seen_dir[0] or last_in_buf)
    # If the assistant actually said goodbye, override to sleep — model often
    # drifts and emits the wrong directive (or none) on close turns.
    if directive == "stay" and assistant_signals_close(clean_full):
        print(f"[dir] override stay->sleep (assistant said goodbye)", flush=True)
        directive = "sleep"
    # If the reply was 100% narration filler (all sentences skipped, nothing
    # real was spoken) AND the model didn't explicitly emit a directive, the
    # model is stalling — drop to sleep instead of looping on filler turns.
    if (directive == "stay" and not _had_directive
            and not content_enqueued[0]):
        print(f"[dir] override stay->sleep (no content, no model directive)", flush=True)
        directive = "sleep"
    print(f"[dir] {directive!r} (model_emitted={_had_directive})", flush=True)
    # Flush whatever is left after the last sentence boundary, minus the directive.
    if clean_full:
        remaining = clean_full[min(spoken_idx[0], len(clean_full)):].strip()
        if remaining and not interrupted.is_set():
            text_q.put(remaining)
    text_q.put(None)  # done sentinel
    synth.join()
    stop_mon.set(); mon.join(timeout=0.5)
    print(f"[t] llm+tts={time.monotonic()-t_start:.2f}s reply_chars={len(clean_full)}", flush=True)

    # Persist clean reply (no directive) in history.
    if clean_full and history and history[-1].get("role") == "assistant":
        if isinstance(history[-1].get("content"), str) and history[-1]["content"] != clean_full:
            history[-1]["content"] = clean_full; _persist_history()

    last_activity[0] = time.time()
    if interrupted.is_set() and directive != "sleep":
        # Real barge-in on a stay-bound reply -> mic stays open for user follow-up.
        _mark_last_assistant_interrupted()
        return "stay"
    if interrupted.is_set() and directive == "sleep":
        # The model already decided to end the session. Barge-in here is almost
        # always TTS-loopback firing on Iva's own "goodnight" message — and even
        # if it was a real interrupt, the user explicitly closed before. Honor
        # sleep; user can re-wake if they want to continue.
        print("[dir] barge-in during sleep turn -> honor sleep (no stay override)", flush=True)
    if not clean_full: return "sleep"
    return directive

print(f"[ready] listening for '{WAKE_LABEL}'...",flush=True)
publish("listening")  # initial wake-only state; `finally` handles all subsequent resets
while True:
    wake_listen(); _state["wakes"]+=1
    publish("wake"); last_activity[0]=time.time()
    try:
        beep_wake()
        # First turn after wake: normal capture timeout (8s).
        next_state = do_turn(start_timeout_s=8)
        # Subsequent turns happen only if the LLM said [[stay]] (or barge-in).
        # They use LISTENING_IDLE_TIMEOUT so we drop back to SLEEP if silent.
        while next_state == "stay":
            publish("followup")
            next_state = do_turn(start_timeout_s=LISTENING_IDLE_TIMEOUT)
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        # Publish the wake-only state up front so the log/UI show the sleep
        # transition immediately, not after the beep + 2s quiet gap.
        publish("listening")
        try: beep_sleep()
        except Exception: pass
        time.sleep(SLEEP_QUIET_S)  # quiet gap before re-arming the wake detector
