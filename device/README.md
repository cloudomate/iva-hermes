# Hermes headless wake-word voice assistant (iva)

A standalone, headless voice loop that turns the Hermes Agent (installed natively
on iva) into a hands-free **"Hey Hermes" / "Hey Iva"** smart speaker, using the
XVF3800 mic array for input and a paired backend host for the heavy lifting. No TUI —
runs as a `systemctl --user` service. An optional `rich` status display can be
shown on an attached screen.

The wake words are custom microWakeWord models trained locally — see
[`training/TRAINING.md`](training/TRAINING.md). The daemon auto-loads every
`*.json` manifest in `WAKE_MODELS_DIR` (`/home/iva/wakewords`, mirrored in
[`wakewords/`](wakewords/)), falling back to bundled `Hey Jarvis` if none found.

```
"Hey Hermes" / "Hey Iva" (microWakeWord, FL channel)
   -> beep -> record-to-silence (FL) -> Whisper STT (backend)
   -> Hermes AIAgent (backend LLM) -> Kokoro TTS (backend) -> play
   -> reply directive ([[stay]] -> mic open / [[sleep]] -> beep, wake-only)
   -> barge-in: user speech during TTS cuts playback and jumps to recording
```

## Components / files (on iva)

| Path | What |
|---|---|
| `/home/iva/hermes_voice_wake.py` | The headless daemon (wake loop). |
| `/home/iva/hermes_voice_display.py` | Optional `rich` status display (reads state file). |
| `/home/iva/wakewords/*.{tflite,json}` | Custom wake models (auto-loaded). See [`wakewords/`](wakewords/) + [`training/`](training/). |
| `~/.config/systemd/user/hermes-voice.service` | systemd user service. |
| `~/.config/systemd/user/hermes-voice.service.d/override.conf` | env (`WAKE_CUTOFF`) + file logging. |
| `/home/iva/voicewake.log` | daemon log (StandardOutput/Error). |
| `$XDG_RUNTIME_DIR/hermes-voice/state.json` | live state the display reads. |
| `/home/iva/.hermes/config.yaml` | Hermes config (LLM/STT/TTS endpoints + `skills.external_dirs`). |
| `/home/iva/.local/bin/iva-volume` | Volume helper (up/down/set/get), persists `~/.config/iva-voice/volume`. |
| `/home/iva/.hermes/SOUL.md` | Device-control contract auto-injected each turn (see [`SOUL.md`](SOUL.md)). |

## Voice skills (device control)

The agent's device-control skills (speaker volume, etc.) live in this repo under
[`../skills/`](../skills) and are consumed by Hermes as a **tap** (or via
`../install.sh` → `skills.external_dirs`). See the [repo README](../README.md)
for both install paths; `git pull`/`hermes skills update` to refresh. Reliable,
low-latency triggering relies on the concrete command contract being pinned in
[`SOUL.md`](SOUL.md) (auto-injected every turn) — the skills are the documented
source of truth. The `iva-volume` helper itself ships here under `device/` (it's
a device binary, not a skill).

## Backends (run on a paired host, reachable from iva as `<backend-host>`)

| Service | Port | What |
|---|---|---|
| Ollama (LLM) | 11434 | OpenAI-compatible `/v1` chat (model is whatever the host loaded) |
| whisper.cpp (STT) | 8003 | OpenAI `/v1/audio/transcriptions` (e.g. `ggml-large-v3-turbo`) |
| Kokoro-FastAPI (TTS) | 8004 | OpenAI `/v1/audio/speech`, voice `af_heart`, 24 kHz |

`<backend-host>` is whatever name resolves to the paired host from iva (mDNS
`.local`, DNS, or a `/etc/hosts` entry); the original setup used a direct link to
a Mac, but any host on the network exposing the three OpenAI-compatible
endpoints works.

## Hermes config.yaml changes (the integration)

```yaml
model:                         # LLM -> backend Ollama
  default: qwen3.5:35b-mlx
  provider: custom
  base_url: http://<backend-host>:11434/v1
  api_key: sk-local
stt:                           # STT -> backend whisper.cpp
  enabled: true
  provider: openai
  openai:
    base_url: http://<backend-host>:8003/v1
    model: whisper-1
    api_key: sk-local          # REQUIRED: without it the resolver falls back to
                               # api.openai.com and 401s (see transcription_tools
                               # _resolve_openai_audio_client_config)
tts:                           # TTS -> backend Kokoro
  provider: openai
  openai:
    base_url: http://<backend-host>:8004/v1
    model: kokoro
    voice: af_heart
    api_key: sk-local
voice:
  silence_threshold: 2500      # above the XVF3800 AGC noise floor
  auto_tts: true               # speak replies (the TUI also has /voice tts)
  beep_enabled: true
```
`.env`: `OPENAI_API_KEY=sk-local` (the audio OpenAI clients read it; Kokoro/whisper ignore the value).

NOTE: the `model:`/`whisper-1`/`kokoro` strings are just OpenAI API labels — the
actual models are whatever each backend server loaded (e.g. large-v3-turbo, kokoro-v1_0).

## venv packages added (voice deps the installer omitted)

```
/home/iva/.hermes/hermes-agent/venv/bin/python -m pip install \
    sounddevice numpy soundfile setuptools pymicro-wakeword
# system: libportaudio2 (already present)
```
`pymicro-wakeword` bundles the `hey_jarvis` microWakeWord model. `faster_whisper`
is NOT needed (STT provider is `openai`, not `local`).

## Key engineering decisions / gotchas (why it's built this way)

1. **Wake input = FL channel of 6-channel capture, NOT the default device.**
   The XVF3800 exposes a 6-channel source (FL FR FC LFE RL RR) under the PipeWire
   `analog-surround-51` profile. sounddevice's default (mono) DOWNMIXES all 6,
   diluting the voice ~5x -> wake never fires. The daemon opens 6ch and extracts
   channel 0 (FL); `WAKE_CH` env selects the channel.

2. **Wake cutoff lowered 0.97 -> 0.5.** pymicro's bundled hey_jarvis model defaults
   to `probability_cutoff=0.97`, too strict for this mic/room. Set via the
   `WAKE_CUTOFF` env in the service override. Tune 0.5-0.7 (lower = more sensitive).

3. **Own single-stream recorder, not Hermes' `AudioRecorder`.** The bundled
   `create_audio_recorder()` opens its own threaded PortAudio stream; opening it
   right after the wake stream closed caused a **hard segfault** (PortAudio
   re-entrancy). `record_to_silence()` is reimplemented in-daemon: one FL stream,
   main thread, RMS-gated silence detection -> writes a WAV.

4. **Reply played via WAV + `sd.play`, not mp3 via ffplay.** Kokoro/Hermes return
   mp3; `play_audio_file` would use ffplay (can pick the wrong device). The daemon
   converts mp3 -> wav (ffmpeg) and uses `sd.play` — the same path as the beeps,
   which is proven to reach the speaker.

5. **Turn wrapped in try/except.** A turn error no longer crashes the daemon
   (systemd would otherwise restart-loop and hide the traceback).

6. **Multi-turn:** conversation history is threaded across turns via
   `AIAgent.run_conversation(text, conversation_history=history)`; reset after
   `HISTORY_IDLE_RESET` (120 s) idle.

## Audio device setup (one-time, persists via PipeWire)

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
# 6-channel capture profile (so FL is available):
pactl set-card-profile alsa_card.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700261100067-00 \
      output:analog-stereo+input:analog-surround-51
# louder playback (hardware PCM is already 0 dB; this is software gain):
wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.4
```

## Tuning knobs (in hermes_voice_wake.py / service env)

- `WAKE_CUTOFF` (env, override.conf) — wake sensitivity (0.5 default).
- `WAKE_CH` (env) — 0=FL, 1=FR.
- `LISTENING_IDLE_TIMEOUT` (12.0) — after `[[stay]]`, how long to wait for
  the user to speak before dropping back to wake-only.
- `BARGE_HITS_NEEDED` (3) — consecutive over-threshold blocks (~80 ms each)
  before TTS playback is interrupted.
- `BARGE_RMS_MULT` (1.5) — multiplier on `speech_threshold()` for the
  barge-in bar; higher = less likely to trip on TTS loopback / room noise.
- `record_to_silence(silence_s=1.5, rms_thresh=speech_threshold(), start_timeout_s=8)`.

## Manage

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart hermes-voice.service
systemctl --user status  hermes-voice.service
tail -f /home/iva/voicewake.log
# show the status display on the attached screen / any terminal:
/home/iva/.hermes/hermes-agent/venv/bin/python /home/iva/hermes_voice_display.py
```

## XVF3800 note
AGC (`PP_AGCMAXGAIN`) was set to 64 (Seeed default) which over-drives close
speech into clipping (bad for STT); ~16-24 is gentler. Read/write needs
`sudo ./xvf_host PP_AGCMAXGAIN <v>` then `SAVE_CONFIGURATION`.

## Long-term memory, adaptive thresholds & persistent history (later additions)

**Long-term memory (durable facts).** `config.yaml` `memory.provider: ''` → **`holographic`**
enables Hermes' built-in SQLite memory provider (`~/.hermes/memory_store.db`) with
automatic per-turn **recall (prefetch)** + **write (sync)**. The daemon constructs
`AIAgent(skip_memory=False, session_id="iva-voice-main", platform="cli",
load_soul_identity=True)`. Facts ("my name is Yash") are auto-extracted, stored, and
recalled across restarts/reboots — independent of conversation length.
- **GOTCHA:** do **NOT** pass `ephemeral_system_prompt` — it *replaces* the system
  prompt and disables the memory injection. The voice-terseness instruction is sent
  as a **user-message prefix** (`VOICE_HINT`) with `persist_user_message=<clean text>`
  so the hint reaches the model but isn't stored in history/memory.
- Two layers: long-term memory (facts, forever) + disk-persisted **conversation
  history** (`/home/iva/.hermes-voice-history.json`, last 60 msgs, recent flow).

**Adaptive RMS thresholds.** Speech/silence thresholds auto-tune to the room:
`noise_floor` is an EMA of FL RMS while listening (only adapts to quiet samples, so
speech doesn't inflate it); `speech_threshold() = max(SPEECH_MIN, noise_floor *
SPEECH_MULT)` (env `SPEECH_MULT`=3.0, `SPEECH_MIN`=450). Used for command-recording
onset/endpoint and (scaled by `BARGE_RMS_MULT`) for the barge-in monitor during
TTS. State JSON now also exposes `noise`/`thresh`.

**LLM-driven session state.** "End of conversation" is decided by the model,
not a fixed acoustic timer. Every reply ends with `[[stay]]` (keep mic open
for follow-up) or `[[sleep]]` (return to wake-only). The directive is stripped
before TTS and before being saved to history. The contract is in
[`SOUL.md`](SOUL.md). Safety net: if the model says `[[stay]]` but no one
speaks for `LISTENING_IDLE_TIMEOUT` seconds, the daemon drops back to SLEEP.

**Barge-in.** TTS playback is non-blocking; a daemon thread monitors the FL
mic with a sustained-RMS gate during playback. On a hit, `sd.stop()` cuts
audio immediately and the next user turn captures the interrupting utterance.
The interrupted assistant message gets a `[interrupted]` suffix in history so
the model doesn't try to resume the old reply.

**Reminder:** `config.yaml`, `.env`, and `memory.provider` live on iva, not in git —
this dir is the source-of-truth copy of the *scripts*; the README documents the
config/venv side.
