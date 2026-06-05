# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Iva** is a hands-free, wake-word ("Okay Iva") voice assistant that runs the
**Hermes Agent** natively on a Raspberry Pi (reSpeaker XVF3800 mic array +
speaker). This repo is the **source-of-truth copy of the on-device app** — it is
*not* the deployment target. Code here is pulled onto the device
(`/home/iva/...`) and into Hermes (`~/.hermes/...`); paths in the scripts are
hardcoded to those device locations, not this repo.

This repo:
- `device/` — the on-device app (wake daemon, helpers, systemd unit, wake-word
  models + training pipeline).
- `install.sh` — pulls the agent skills onto a device and registers them in
  `config.yaml` (`skills.external_dirs`).

The **agent skills live in a separate repo**,
[`cloudomate/skills`](https://github.com/cloudomate/skills) (anthropics/skills-style
monorepo). They are consumed by the device as a "tap" or via `skills.external_dirs`.

There is **no build/test/lint tooling** in this repo — it's deployment scripts,
Python daemons that run on the Pi, and Markdown skill/doc files. Validate Python
with `python3 -m py_compile device/*.py` and shell with `bash -n device/*.sh`.

## The runtime pipeline (device/hermes_voice_wake.py)

The single most important file. A headless `systemctl --user` daemon implementing:

```
wake (microWakeWord, FL channel) -> beep -> record-to-silence (FL)
  -> Whisper STT (backend) -> Hermes AIAgent (backend LLM) -> Kokoro TTS (backend)
  -> play -> 5s hands-free follow-up window -> sleep (beep)
```

Heavy models (LLM/STT/TTS) run on a **paired backend host** reached over the
network; the Pi only runs the wake loop, orchestration, and hardware control.
The daemon publishes live state to `$XDG_RUNTIME_DIR/hermes-voice/state.json`,
which `hermes_voice_display.py` (optional `rich` UI) reads.

### Non-obvious design constraints (read before editing the daemon)

These are hard-won and easy to regress — the device README documents the full
list, but the critical ones:

1. **Wake input is the FL channel of a 6-channel capture, not the default
   device.** The XVF3800's PipeWire `analog-surround-51` profile exposes 6
   channels (FL FR FC LFE RL RR). sounddevice's default mono *downmixes* all 6,
   diluting the voice ~5× so wake never fires. The daemon opens 6ch and extracts
   channel 0 (`WAKE_CH`). `ensure-xvf-profile.sh` forces this profile at boot.
2. **Do NOT use Hermes' bundled `create_audio_recorder()`.** Opening its threaded
   PortAudio stream right after the wake stream closed caused a **hard segfault**
   (PortAudio re-entrancy). `record_to_silence()` is a deliberate
   reimplementation: one FL stream, main thread, RMS-gated.
3. **Replies play as WAV via `sd.play`, not mp3 via ffplay.** mp3 is converted to
   wav with ffmpeg, then played the same way as the beeps (proven to reach the
   speaker); ffplay can pick the wrong device.
4. **Memory/voice-hint coupling.** Long-term memory injection is enabled via
   `AIAgent(skip_memory=False, ...)`. Do **NOT** pass `ephemeral_system_prompt` —
   it *replaces* the system prompt and disables memory injection. The voice
   terseness instruction is sent as a **user-message prefix** (`VOICE_HINT`) with
   `persist_user_message=<clean text>` so the hint reaches the model but isn't
   stored in history/memory.
5. **Adaptive RMS thresholds** auto-tune to the room: `noise_floor` is an EMA of
   FL RMS that only adapts to quiet samples (so speech doesn't inflate it).

### Tuning knobs (env, mostly via the systemd override)

`WAKE_CUTOFF` (wake sensitivity, lower = more sensitive), `WAKE_CH` (0=FL, 1=FR),
`WAKE_MODELS_DIR`, `SPEECH_MULT`/`SPEECH_MIN` (adaptive threshold),
`FOLLOWUP_SECONDS`, `WAKE_DEBUG=1` (logs wake `peak prob_mean` for diagnosing
voice-mismatch vs cutoff problems).

## Skills (now in cloudomate/skills)

The agent skills moved to [`cloudomate/skills`](https://github.com/cloudomate/skills).
There they stay **flat** (`skills/<name>/SKILL.md`) so the skills.sh tap enumerator
discovers them; categories come from that repo's `skills.sh.json`.

The concrete command contract for each capability (e.g. the exact `iva-volume`
sub-commands) is **pinned in places that must stay in sync** — and they now span
two repos: in `cloudomate/skills` both `skills/<name>/SKILL.md` and the umbrella
`skills/iva-hermes/SKILL.md` table; in **this** repo `device/SOUL.md`. `SOUL.md`
is auto-injected into Hermes every turn — that redundancy is intentional and is
what makes low-latency voice device-control reliable. When you change a helper's
interface, update all of them (across both repos).

## Device helpers

`device/iva-volume` is a **device binary, not a skill** (it ships under `device/`,
runs on the Pi at `/home/iva/.local/bin/iva-volume`). It wraps `wpctl` but
**persists** the level to `~/.config/iva-voice/volume`, which
`ensure-xvf-profile.sh` restores at boot. The persistence is the whole point —
skills and SOUL.md both forbid calling `wpctl`/`pactl` directly because those
don't survive a reboot.

## Wake-word training (device/training/)

Custom microWakeWord models are trained on a Mac with
[OHF-Voice/micro-wake-word](https://github.com/OHF-Voice/micro-wake-word); the
quantized `.tflite` + `.json` manifests land in `device/wakewords/` (mirrored to
`/home/iva/wakewords/` on the device). The daemon auto-loads every `*.json` there,
falling back to bundled `hey_jarvis`. **Okay Iva** is the active model. See
`device/training/TRAINING.md` for the full pipeline, the required numpy/torch
patches, and the empirical wake-word design lessons (syllable count dominates
accuracy; synthetic Piper audio often mismatches the real speaker → train on real
recordings). The `prep_*.py` scripts and `training_parameters_*.yaml` are reusable
templates.

## Deploy / manage

```bash
# Install/update skills (from cloudomate/skills) on a device and wire them into config.yaml:
curl -fsSL https://raw.githubusercontent.com/cloudomate/iva-hermes/main/install.sh | bash
# or the native tap: hermes skills tap add cloudomate/skills

# On the device:
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart hermes-voice.service
systemctl --user status  hermes-voice.service
tail -f /home/iva/voicewake.log
```

## Secrets / config boundary

**Public repo — no secrets.** Backend endpoints and API keys live only in the
device's `~/.hermes/config.yaml` and `.env` (LLM/STT/TTS base_urls + the
`api_key: sk-local` placeholder that the local backends ignore but the OpenAI
client requires). These files are **not in git**; the device README documents
their expected shape but the values stay on the device. `memory.provider:
holographic` (SQLite at `~/.hermes/memory_store.db`) and the conversation-history
file (`~/.hermes-voice-history.json`) also live only on the device.
