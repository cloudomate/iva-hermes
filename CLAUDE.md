# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Iva** is a hands-free, wake-word ("Okay Iva") voice assistant that runs the
**Hermes Agent** natively on a Raspberry Pi (reSpeaker XVF3800 mic array +
speaker). This repo is the **source-of-truth copy of the on-device app** — it is
*not* the deployment target. Code here is pulled onto the device
(`/home/iva/...`) and into Hermes (`~/.hermes/...`); paths in the scripts are
hardcoded to those device locations, not this repo.

This repo is a **Python package** (`iva`, src layout):
- `src/iva/` — the on-device app as an importable package. `cli.py` is the
  single **`iva`** entry point (`iva run` = the daemon, `iva install`, `iva
  display`, `iva volume`, `iva audio`, `iva devices`). Modules: `wake.py` (daemon),
  `router.py` (tier-1 no-tools 12B triage), `display.py`, `audio.py` + `volume.py`
  (also exposed as `iva-audio`/`iva-volume` aliases), `narration_filter.py`,
  `speaker_gate.py`, `service.py` (the `iva install` systemd setup), `devices.py`,
  and `data/SOUL.md` (package data).
  Greenfield is `pip install iva-hermes && iva run` — no repo clone; deps pull
  `hermes-agent` + the audio stack (`pymicro-wakeword` = the microWakeWord
  runtime). The wake-word **models** are bundled here as package data
  (`src/iva/data/wakewords/`). `iva install` is *optional* (a systemd --user unit
  whose `ExecStart` is `iva run`). The training pipeline that makes new models
  lives in `cloudomate/iva-wakeword` (training-only, not a runtime dep).
- `deploy/` — non-Python deploy artifacts: the systemd unit + override,
  `ensure-xvf-profile.sh`, and `install-device.sh` (pip-installs the package
  into the Hermes venv, symlinks the entry points to `~/.local/bin`, installs
  the unit + wake-word models).
- `src/iva/ble/` — the **companion-app setup service** (`iva-ble`, systemd unit
  `iva-ble.service`): a bless GATT peripheral ("Iva Setup") speaking JSON-RPC —
  `rpc.py` (transport-agnostic dispatch + **passwordless** auth in `auth.py`:
  proximity-at-setup trust — the first client claims the device with
  `auth.pair` and holds a durable secret; more phones via a 60 s
  `auth.pair_window`; the web console via a one-time 6-digit `auth.web_code`),
  `handlers.py` (status/wifi/models/wakeword/audio/volume/bluetooth/net.info/
  device.reset/apply, plus `device.set_timezone`/`device.timezone` — the app
  pushes its IANA timezone on connect so Iva's clock/reminders/cron match the
  owner; writes the Hermes `timezone` config that `hermes_time.py` reads, no
  sudo, effective next agent restart). The broadcast is **pairing-aware** (`server.py`): it
  stops once a phone holds a pairing secret and the device is online (the app
  uses the LAN API), and resumes when the network drops (BLE rescue) or after
  a factory reset (`iva reset`, the `device.reset` RPC, or a GPIO button —
  `reset_button.py`, armed by `IVA_RESET_GPIO`; wipe semantics in `reset.py`).
  The Flutter app `cloudomate/iva-app` is the BLE central; UUIDs must stay in
  sync with its `lib/ble/protocol.dart`.
- `src/iva/api/` — the **web-console service** (`iva-api`, `iva-api.service`,
  port **8800**): FastAPI second transport over the SAME rpc/auth/handlers —
  `POST /rpc`, `WS /chat` (streaming text turns through the same AIAgent setup
  + history file as the voice daemon: one conversation across voice and web),
  extra shared actions `chat.history` + `chat.send` (non-streaming turn,
  accepts base64 `image` and `audio` voice-note attachments). **Image turns
  bypass the agent** and call the vision model directly (`_vision_reply`):
  image-before-text, `enable_thinking:false` (else gemma buries the answer in
  `reasoning_content` and returns empty), because the agent's own image
  pipeline (`auxiliary.vision`) isn't wired for the custom provider and 502s/
  times out. Audio is Whisper-transcribed then runs as a normal agent turn. `skills.*` (incl. `skills.enable`/
  `skills.disable`, which edit the `skills.disabled` list in
  `~/.hermes/config.yaml` — the list Hermes' skills loader reads), and
  `skills.sources` (registry picker for `skills.search --source`),
  `skills.github_token`/`skills.set_github_token` (GITHUB_TOKEN in `.env` for
  private/rate-limited GitHub search+install — the web console's "Find new
  skills" searches public + private and installs individual skills; whole-repo
  `skills.taps`/`tap_add`/`tap_remove` RPCs still exist but the console UI for
  them was dropped in favor of per-skill install). `skills.install` parses the
  real outcome (`hermes skills install` exits 0 even when it no-ops): returns
  `installed`/`blocked`(security-scan caution, needs `force:true`)/`already`,
  `tools.*` (toolset enable/disable via `hermes tools`; enable also runs
  `hermes doctor --fix` to pull in add-ons), `gateway.*`, and `dev.exec` (the
  web console's developer terminal — also a full PTY shell at `WS /term`)
  (`actions.py`), and statically serves the SPA from `cloudomate/iva-web`
  (`~/.local/share/iva-web`). The API
  serves **HTTPS by default** (`api/tls.py`: self-signed EC cert in
  `~/.config/iva-voice/tls/`, `IVA_API_TLS=0` for plain HTTP) so LAN traffic is
  encrypted; the app pins the cert's SHA-256 fingerprint, learned over BLE via
  the `net.info` action (BLE→LAN trust bootstrap). Tokens are **shared across
  transports** (hashes + expiry in `~/.config/iva-voice/tokens.json`, see
  `ble/auth.py`): a BLE login is valid on the API and survives restarts.
- `pyproject.toml` — packaging (hatchling); entry points `iva-audio`, `iva-volume`,
  `iva-ble`, `iva-api`; extras `ble` (bless) and `api` (fastapi, uvicorn).
- `install.sh` — separate concern: pulls the agent skills (cloudomate/skills)
  onto a device and registers them in `config.yaml` (`skills.external_dirs`).

The **agent skills live in a separate repo**,
[`cloudomate/skills`](https://github.com/cloudomate/skills) (anthropics/skills-style
monorepo). They are consumed by the device as a "tap" or via `skills.external_dirs`.

Build with `uv build` (or `python -m build`) — hatchling, src layout. There is
no test/lint tooling; validate Python with `python3 -m py_compile src/iva/*.py`
and shell with `bash -n deploy/*.sh`. The daemon's runtime also needs the ambient
**Hermes Agent** (`run_agent`, `tools.*` at `~/.hermes/hermes-agent`), which is
NOT a pip dependency — `iva.wake` injects that path on `sys.path` itself.

## The runtime pipeline (src/iva/wake.py — `iva run`)

The single most important module. A headless `systemctl --user` daemon implementing:

```
wake (microWakeWord, FL channel) -> beep -> record-to-silence (FL)
  -> 12B audio router, no tools (iva/router.py — ONE call: ASR + route + reply):
       answer -> speak reply directly | escalate -> speak "Ok, let me ... for you."
       (Whisper STT is the fallback when the router is off / didn't hear it)
  -> Hermes AIAgent with tools (escalate only, gets the transcript text)
  -> Kokoro TTS (backend)
  -> play (barge-in armed) -> reply directive: [[stay]] keeps mic open
                                              [[sleep]] beeps + back to wake
```

The **two-call router** (`router.py`, `IVA_ROUTER=0` disables) makes at most
two calls to the SAME 12B per turn: call 1 sends the turn WAV with a tiny
no-tools prompt — ASR, answer-vs-escalate, and the spoken reply/ack blended
into one call; call 2 is the full tool-equipped agent (text in), run only on
escalate while the ack plays. Single-turn audio works on the 12B *because* the
router prompt is tiny — the earlier one-pass attempt failed by drowning the
audio attention in the full tool-laden context (that lesson built the interim
E4B PA tier, now removed along with `personal_assistant.py`). The 12B server
loads the thin unified-arch `mmproj-12B-F16.gguf` (projection layers only) to
accept audio. The two prompt shapes occupy different llama.cpp slots, so both
KV-cache prefixes stay warm (default `--parallel 4`; both slots are primed at
startup by `_warmup_llm` + `_warmup_router`).

Heavy models (LLM/STT/TTS) run on a **paired backend host** reached over the
network; the Pi only runs the wake loop, orchestration, and hardware control.
The daemon publishes live state to `$XDG_RUNTIME_DIR/hermes-voice/state.json`,
which `iva.display` (optional `rich` UI) reads.

### Non-obvious design constraints (read before editing the daemon)

These are hard-won and easy to regress — the device README documents the full
list, but the critical ones:

1. **Audio is generic (PipeWire/sounddevice) and config-driven; multi-channel
   arrays must be pinned.** Resolution lives in `iva/audio_config.py`: a named
   **preset** supplies defaults (`generic` = system default + mono; built-in
   `respeaker-xvf3800` = 6ch + extract ch0), chosen via `IVA_AUDIO_PRESET` /
   `~/.config/iva-voice/audio.yaml` (`preset:`) / `iva run --preset`. The user
   file can define custom presets + `overrides:`. Env always wins:
   `AUDIO_SOURCE`/`AUDIO_SINK` (name substring or index → `sd.default.device`),
   `AUDIO_CHANNELS` (capture count; unset = auto-try 6 then mono), `WAKE_CH`
   (channel `read_fl()` extracts when channels > 1). **XVF3800 is one such preset:** its `analog-surround-51`
   profile exposes 6ch (FL FR FC LFE RL RR) whose default mono *downmixes* +
   dilutes the voice ~5×, so set `AUDIO_CHANNELS=6 WAKE_CH=0` and force the
   profile with `ensure-xvf-profile.sh`. Use `iva devices` to find node names.
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
6. **LLM controls the ears.** There is no acoustic followup timer anymore. Each
   reply ends with `[[stay]]` or `[[sleep]]` (contract in `SOUL.md`), parsed
   and stripped by `parse_directive()` before TTS. `[[stay]]` reopens the mic
   with no wake gate; `[[sleep]]` returns to wake-only. Safety net:
   `LISTENING_IDLE_TIMEOUT` (12 s) drops back to wake-only if the model says
   `[[stay]]` but no one speaks. Don't reintroduce a fixed `FOLLOWUP_SECONDS`
   window — the whole point of the redesign was to remove acoustic guessing.
7. **Barge-in concurrency.** `speak()` is non-blocking and a daemon thread
   monitors the FL mic with a sustained-RMS gate during playback (constraint
   #2's segfault was input-stream → input-stream re-entry; input + output
   concurrent under PipeWire works). On a hit, `sd.stop()` cuts TTS and the
   next user turn captures the interrupting utterance; the partial assistant
   message gets a `[interrupted]` suffix in history so the model knows not to
   resume the previous reply.

### Tuning knobs (env, mostly via the systemd override)

`IVA_ROUTER` (voice-input mode: unset/1 = multimodal one-call audio router,
0 = ASR/Whisper + single-call agent; exposed to the companion app + web console
as `voice.mode` via `models.get`/`models.set`, stored in the env override),
`WAKE_CUTOFF` (wake sensitivity, lower = more sensitive), `WAKE_CH` (0=FL, 1=FR),
`WAKE_MODELS_DIR`, `SPEECH_MULT`/`SPEECH_MIN` (adaptive threshold),
`LISTENING_IDLE_TIMEOUT` (stay-mode dropback, 12 s), `BARGE_HITS_NEEDED` /
`BARGE_RMS_MULT` (barge-in monitor tuning), `WAKE_DEBUG=1` (logs wake `peak
prob_mean` for diagnosing voice-mismatch vs cutoff problems).

## Skills (now in cloudomate/skills)

The agent skills moved to [`cloudomate/skills`](https://github.com/cloudomate/skills).
There they stay **flat** (`skills/<name>/SKILL.md`) so the skills.sh tap enumerator
discovers them; categories come from that repo's `skills.sh.json`.

The concrete command contract for each capability (e.g. the exact `iva-volume`
sub-commands) is **pinned in places that must stay in sync** — and they now span
two repos: in `cloudomate/skills` both `skills/<name>/SKILL.md` and the umbrella
`skills/iva-hermes/SKILL.md` table; in **this** repo `src/iva/data/SOUL.md`. `SOUL.md`
is auto-injected into Hermes every turn — that redundancy is intentional and is
what makes low-latency voice device-control reliable. When you change a helper's
interface, update all of them (across both repos).

## Device helpers

`iva.volume` / `iva.audio` / `iva.spotify` are **device helpers, not skills** —
package modules exposed as the `iva-volume` / `iva-audio` / `iva-spotify`
console entry points and symlinked to
`/home/iva/.local/bin/` (the path SOUL.md and the skills reference). `iva-volume`
wraps `wpctl` but **persists** the level to `~/.config/iva-voice/volume`, which
`ensure-xvf-profile.sh` restores at boot. The persistence is the whole point —
skills and SOUL.md both forbid calling `wpctl`/`pactl` directly because those
don't survive a reboot. (`iva-volume` was a bash script; it's now a
behaviour-preserving Python port, `IVA_VOL_MAX` default 2.50.)

**Messaging gateway** — `iva/gateway.py` + `gateway.*` RPC actions: a
declarative `PLATFORMS` spec (Telegram/Discord/Slack/WhatsApp/Teams/Google
Chat/Gmail), each a set of `~/.hermes/.env` vars (WhatsApp is QR-paired via
`hermes whatsapp` in the Developer terminal). Generic get/set over the env
file (reuses `integrations._set_env_file`; secrets never returned to the
client); the gateway service is driven via `hermes gateway install/start/
stop/status`. The web console renders every platform from the spec (Gateway
tab).

**Email (Himalaya CLI)** — `iva/himalaya.py` + `email.*` RPC actions: assisted
setup for the `himalaya` skill (agent reads/sends your mailbox), DISTINCT from
the Gmail *gateway* adapter (which lets people email the agent). Installs the
binary if missing and generates `~/.config/himalaya/config.toml` from an email
+ app password (IMAP/SMTP auto-filled for Gmail/Outlook/Yahoo/iCloud, with the
Gmail folder aliases that are otherwise a save-to-Sent footgun).

**Integrations (assisted sign-up)** — `iva/integrations.py` + the
`integrations.*` RPC actions: Home Assistant connects with the user's normal
HA login (device runs HA's `login_flow`, mints a long-lived token over HA's
WebSocket, writes `HASS_URL`/`HASS_TOKEN` into `~/.hermes/.env`); Spotify uses
Authorization Code + **PKCE** with a shared product client_id (no secret) —
config in `~/.hermes/integrations/spotify.json` (read by `iva-spotify`,
**survives factory reset**; legacy `~/.config/iva-voice/spotify.json` is
migrated). The app drives Spotify consent via the `cc.cloudomate.iva://spotify`
redirect; the web console uses `http://127.0.0.1:8898/callback` + paste-back —
both URIs must be registered in the Spotify app dashboard.

## Wake words: runtime vs training

**Runtime** = `pymicro-wakeword` (the microWakeWord inference engine, a pip dep).
The active **models** ship bundled in this package at `src/iva/data/wakewords/`;
the daemon auto-loads every `*.json` in `WAKE_MODELS_DIR` (default falls back to
that bundled dir, then to pymicro's `hey_jarvis`). **Okay Iva** is active.

**Training** lives in a separate repo,
[`cloudomate/iva-wakeword`](https://github.com/cloudomate/iva-wakeword) — the
pipeline (run on a Mac with
[OHF-Voice/micro-wake-word](https://github.com/OHF-Voice/micro-wake-word)) and
the trained artifacts. It is **training-only, not a runtime dependency**. To ship
a new wake word, copy its produced `.tflite` + `.json` into
`src/iva/data/wakewords/` here and release. See that repo's `training/TRAINING.md`
for the pipeline and the empirical design lessons (syllable count dominates
accuracy; synthetic Piper audio often mismatches the real speaker → train on real
recordings).

## Deploy / manage

```bash
# Install/run the device app (greenfield — no clone). Pulls hermes-agent + audio
# stack; wake models are bundled in the package:
pip install iva-hermes && iva run
# optional autostart on boot: `iva install` (systemd --user unit, ExecStart=iva run)
# helpers: iva volume up | iva audio record 5 | iva devices | iva presets

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
