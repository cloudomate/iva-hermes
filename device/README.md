# Hermes headless wake-word voice assistant (iva)

A standalone, headless voice loop that turns the Hermes Agent (installed natively
on iva) into a hands-free **"Hey Hermes" / "Hey Iva"** smart speaker, using the
XVF3800 mic array for input and Mac-hosted models for the heavy lifting. No TUI —
runs as a `systemctl --user` service. An optional `rich` status display can be
shown on an attached screen.

The wake words are custom microWakeWord models trained locally — see
[`training/TRAINING.md`](training/TRAINING.md). The daemon auto-loads every
`*.json` manifest in `WAKE_MODELS_DIR` (`/home/iva/wakewords`, mirrored in
[`wakewords/`](wakewords/)), falling back to bundled `Hey Jarvis` if none found.

```
"Hey Hermes" / "Hey Iva" (microWakeWord, FL channel)
   -> beep -> record-to-silence (FL) -> Whisper STT (Mac)
   -> Hermes AIAgent (Mac Ollama) -> Kokoro TTS (Mac) -> play
   -> 5s hands-free follow-up window -> sleep (beep)
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

## Backends (run on the Mac, reachable from iva as `ys-mbp-01.local`)

| Service | Port | What |
|---|---|---|
| Ollama (LLM) | 11434 | `qwen3.5:35b-mlx` (OpenAI-compatible `/v1`) |
| whisper.cpp (STT) | 8003 | `ggml-large-v3-turbo`, OpenAI `/v1/audio/transcriptions` |
| Kokoro-FastAPI (TTS) | 8880 | OpenAI `/v1/audio/speech`, voice `af_heart`, 24 kHz |

iva reaches the Mac over the direct link (`ys-mbp-01.local` -> 192.168.2.1).

## Hermes config.yaml changes (the integration)

```yaml
model:                         # LLM -> Mac Ollama
  default: qwen3.5:35b-mlx
  provider: custom
  base_url: http://ys-mbp-01.local:11434/v1
  api_key: sk-local
stt:                           # STT -> Mac whisper.cpp
  enabled: true
  provider: openai
  openai:
    base_url: http://ys-mbp-01.local:8003/v1
    model: whisper-1
    api_key: sk-local          # REQUIRED: without it the resolver falls back to
                               # api.openai.com and 401s (see transcription_tools
                               # _resolve_openai_audio_client_config)
tts:                           # TTS -> Mac Kokoro
  provider: openai
  openai:
    base_url: http://ys-mbp-01.local:8880/v1
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
actual models are whatever each Mac server loaded (large-v3-turbo, kokoro-v1_0).

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
- `FOLLOWUP_SECONDS` (5.0) — hands-free window after a reply.
- `FOLLOWUP_RMS` (1000) — speech threshold to chain a follow-up.
- `record_to_silence(silence_s=1.5, rms_thresh=800, start_timeout_s=8)`.

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
onset/endpoint and the follow-up window. State JSON now also exposes `noise`/`thresh`.

**Reminder:** `config.yaml`, `.env`, and `memory.provider` live on iva, not in git —
this dir is the source-of-truth copy of the *scripts*; the README documents the
config/venv side.
