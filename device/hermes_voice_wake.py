#!/usr/bin/env python3
"""Headless wake-word voice assistant for Hermes (iva).
Wake (custom microWakeWord) on FL channel (beep) -> record -> whisper -> agent -> Kokoro
TTS -> play -> 5s follow-up -> sleep (beep). Multi-turn; publishes state JSON.
"""
import os, sys, json, time, threading, subprocess, glob
HERMES = "/home/iva/.hermes/hermes-agent"
sys.path.insert(0, HERMES); os.chdir(HERMES)
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
import numpy as np, sounddevice as sd
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model

RATE = 16000; BLOCK = 1280
WAKE_CH = int(os.environ.get("WAKE_CH", "0"))     # 0=FL, 1=FR (XVF3800 6ch: FL FR FC LFE RL RR)
WAKE_CUTOFF = float(os.environ.get("WAKE_CUTOFF", "0.5"))
FOLLOWUP_SECONDS = 5.0; FOLLOWUP_RMS = 1000.0
# --- adaptive noise floor: auto-tune speech/silence thresholds to the room ---
noise_floor = [250.0]                                  # EMA of ambient FL RMS
NOISE_ALPHA = 0.05
SPEECH_MULT = float(os.environ.get("SPEECH_MULT", "3.0"))   # speech = floor * this
SPEECH_MIN  = float(os.environ.get("SPEECH_MIN", "450"))    # absolute floor
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
    print(f"[state] {_state['state']}"+(f" user={kw['user']!r}" if 'user' in kw else "")
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
WAKE_MODELS_DIR = os.environ.get("WAKE_MODELS_DIR", "/home/iva/wakewords")
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
# Voice terseness is injected as a USER-message prefix (not ephemeral_system_prompt,
# which REPLACES the system prompt and disables Hermes\' long-term memory injection).
VOICE_HINT = ("[Voice mode: if the user asks you to DO something on this device "
              "(e.g. change the volume), actually use your tools / run the command "
              "FIRST and verify it succeeded \u2014 never claim an action you did not "
              "perform. Then reply in one or two short spoken sentences, plain text: "
              "no markdown, emoji, or bullet lists.] ")
agent=AIAgent(model=m.get("default"),base_url=m.get("base_url"),api_key=m.get("api_key") or "sk-local",
    provider=m.get("provider","custom"),quiet_mode=True,skip_memory=False,
    session_id="iva-voice-main", platform="cli", load_soul_identity=True)
HISTORY_FILE="/home/iva/.hermes-voice-history.json"
try:
    history=json.load(open(HISTORY_FILE)); print(f"[init] loaded {len(history)} history msgs from disk",flush=True)
except Exception:
    history=[]
last_activity=[0.0]

def open_input():
    """Open input; prefer 6ch (to extract FL), fall back to mono."""
    for ch in (6,1):
        try:
            s=sd.RawInputStream(samplerate=RATE,channels=ch,dtype="int16",blocksize=BLOCK); s.start(); print(f"[input] opened {ch}ch",flush=True); return s,ch
        except Exception as e:
            print(f"[input] {ch}ch failed: {e}",flush=True)
    raise RuntimeError("no input stream")

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

def listen_for_speech(timeout_s):
    s,ch=open_input()
    try:
        end=time.monotonic()+timeout_s
        while time.monotonic()<end:
            a=np.frombuffer(read_fl(s,ch),np.int16).astype(np.float32)
            if a.size and float(np.sqrt(np.mean(a*a)))>=speech_threshold(): return True
    finally:
        try: s.stop(); s.close()
        except Exception: pass
    return False

def record_to_silence(max_s=15, silence_s=1.5, start_timeout_s=8, rms_thresh=None):
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

def speak(text):
    try:
        r=json.loads(text_to_speech_tool(text=text))
        if not r.get("success"): print("[tts] failed:",r.get("error"),flush=True); return
        mp3=r["file_path"]; wav=mp3.rsplit(".",1)[0]+".play.wav"
        subprocess.run(["ffmpeg","-y","-loglevel","error","-i",mp3,"-ar","24000","-ac","1",wav],check=False)
        import soundfile as _sf; data,sr=_sf.read(wav,dtype="int16"); sd.play(data,sr); sd.wait()
    except Exception as e: print("[tts] error:",e,flush=True)

def do_turn():
    global history
    publish("recording"); wav=record_to_silence()
    if not wav: publish("listening"); return False
    publish("transcribing"); r=transcribe_audio(wav); text=(r.get("transcript") or "").strip()
    if not text or is_whisper_hallucination(text):
        print(f"[turn] no usable speech ({text!r})",flush=True); publish("listening"); return False
    publish("thinking",user=text); reply=""
    try:
        result=agent.run_conversation(VOICE_HINT+text, conversation_history=history, persist_user_message=text)
        reply=(result.get("final_response") or "").strip()
        if result.get("messages"):
            history=result["messages"][-60:]
            try: json.dump(history, open(HISTORY_FILE,"w"))
            except Exception as _e: print("[hist] save failed:",_e,flush=True)
    except Exception: import traceback; traceback.print_exc()
    _state["turns"]+=1
    if not reply: publish("listening"); return True
    publish("speaking",user=text,reply=reply); speak(reply); last_activity[0]=time.time(); return True

print(f"[ready] listening for '{WAKE_LABEL}'...",flush=True)
while True:
    publish("listening"); wake_listen(); _state["wakes"]+=1
    pass  # history is capped + persisted in do_turn (survives restart)
    publish("wake"); last_activity[0]=time.time()
    try:
        beep_wake()
        spoke=do_turn()
        while spoke:
            publish("followup")
            spoke=do_turn() if listen_for_speech(FOLLOWUP_SECONDS) else False
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        try: beep_sleep()
        except Exception: pass
