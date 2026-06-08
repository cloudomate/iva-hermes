# iva-hermes

**Iva** — a hands-free, wake-word voice assistant running the Hermes Agent
natively on a Raspberry Pi (reSpeaker XVF3800 mic array + speaker). Wake word
**"Okay Iva"**; heavy models (LLM / STT / TTS) run on a paired backend host.

This repo holds the **on-device app**, packaged as the `iva` Python package
(src layout). The agent skills live in a dedicated repo —
[`cloudomate/skills`](https://github.com/cloudomate/skills):

```
src/iva/                     # the `iva` package
  wake.py                    #   headless wake->STT->agent->TTS daemon  (python -m iva.wake)
  display.py                 #   optional rich status display           (python -m iva.display)
  audio.py                   #   mic record / playback helper            (entry point: iva-audio)
  volume.py                  #   persisted volume helper                 (entry point: iva-volume)
  narration_filter.py        #   filler/narration detector (used by wake)
  speaker_gate.py            #   opt-in speaker-verification gate
  data/SOUL.md               #   device-control contract (auto-injected each turn)

deploy/                      # non-Python deploy artifacts
  hermes-voice.service(.d)   #   systemd user unit + override (WAKE_CUTOFF etc.)
  ensure-xvf-profile.sh      #   boot: XVF3800 6ch profile + restore volume
  install-device.sh          #   pip-install into Hermes venv + wire up unit/entry points
docs/device.md               # full device setup, backends, gotchas
pyproject.toml               # packaging (hatchling); entry points iva-audio, iva-volume
install.sh                   # (separate) pull cloudomate/skills + register external_dirs
```

## Install & run (device app)

> Full step-by-step (system packages, backend config, audio, autostart,
> troubleshooting): **[docs/running-on-linux.md](docs/running-on-linux.md)**.

Greenfield — no repo clone:

```bash
pip install iva-hermes     # pulls hermes-agent + the audio stack (wake models bundled)
iva run                    # start the assistant: wake -> STT -> agent -> TTS
```

That's the whole thing — `iva run` *is* the assistant. To run it on boot as a
background service instead, optionally:

```bash
iva install                # writes + enables a systemd --user unit whose ExecStart is `iva run`
```

### CLI

```
iva run [--preset NAME]       # the wake -> STT -> agent -> TTS daemon (the service)
iva config show|set ...       # set the LLM / STT (ASR) / TTS endpoints
iva doctor                    # check deps (Python + system) and how to fix
iva presets                   # list device presets + the active one
iva devices                   # list audio sources/sinks
iva volume up|down|set N|get
iva audio record [SECONDS]|play FILE|play-last
iva display                   # optional rich status UI (needs [display])
iva install                   # optional: systemd --user service (ExecStart=iva run)
```

### Configure the models (LLM / ASR / TTS)

Point Iva at your backends without hand-editing YAML:

```bash
iva config set llm.url=http://HOST:11434/v1 llm.model=gemma4:12b-mlx
iva config set stt.url=http://HOST:8000/v1  stt.model=Qwen3-ASR
iva config set tts.url=http://HOST:8000/v1  tts.model=kokoro tts.voice=af_heart
iva config show
```

(Writes `~/.hermes/config.yaml`. Keys: `llm.{url,model,key,provider}`,
`stt.{url,model,key}`, `tts.{url,model,voice,key}`.)

### Startup warmup (no first-turn delay)

On start/restart the daemon initializes the agent and **primes the LLM before it
starts listening**, so the first "Okay Iva" isn't slow. Control with `IVA_WARMUP`:
`sync` (default — block until primed, retrying while the backend comes up),
`async` (prime in the background), or `off`.

### Audio — any source/sink via PipeWire

Defaults to the **system default** mic/speaker with a mono capture, so an
ordinary USB mic/speaker just works (`generic` preset). Known devices have a
**named preset** that supplies the right defaults:

```bash
iva presets                        # list presets + show the active one
iva run --preset respeaker-xvf3800 # 6ch capture, extract FL — no manual tuning
iva devices                        # find your source/sink names/indices
```

Built-in: `generic`, `respeaker-xvf3800`. Select a preset via `iva run
--preset`, `IVA_AUDIO_PRESET=`, or `~/.config/iva-voice/audio.yaml`.

**Your own defaults / custom devices** — `~/.config/iva-voice/audio.yaml`:

```yaml
preset: respeaker-xvf3800     # which preset to use by default
presets:                       # (optional) define your own
  my-usb-array:
    source: "USB Audio"        # device name substring or index
    channels: 4
    wake_channel: 1
overrides:                      # (optional) force values regardless of preset
  sink: "Headphones"
```

Env always wins over the file/preset: `AUDIO_SOURCE`, `AUDIO_SINK`,
`AUDIO_CHANNELS`, `WAKE_CH`. (The XVF3800's 6-channel surround capture downmixes
to mono by default and dilutes the voice ~5×, which is why it needs `channels:
6`; run `deploy/ensure-xvf-profile.sh` to force its `analog-surround-51` profile.)

Wake-word models (microWakeWord; scored by the `pymicro-wakeword` runtime) ship
**bundled** with `iva-hermes`; override the directory with `WAKE_MODELS_DIR`.
Train new ones in [`iva-wakeword`](https://github.com/cloudomate/iva-wakeword)
and copy the `.tflite`/`.json` into `src/iva/data/wakewords/`.

## Skills

The agent's skills (e.g. `volume-control`, the `iva-hermes` capabilities
umbrella) now live in **[`cloudomate/skills`](https://github.com/cloudomate/skills)**.
They are pulled directly onto devices. Either:

```bash
# native tap (browse/install/update via the Skills Hub):
hermes skills tap add cloudomate/skills
hermes skills install cloudomate/skills/skills/volume-control

# or in-place (clone cloudomate/skills + register in config.yaml skills.external_dirs):
curl -fsSL https://raw.githubusercontent.com/cloudomate/iva-hermes/main/install.sh | bash
```

The concrete command contract for each capability (e.g. `iva-volume up`) is
pinned both in the skill's `SKILL.md` (in `cloudomate/skills`) and in
`src/iva/data/SOUL.md` here, which Hermes auto-injects every turn — that
redundancy is what makes voice device-control reliable.

## Device app

See [`docs/device.md`](docs/device.md) for the full setup: backends,
PipeWire/XVF3800 config, wake-word training, and the hard-won gotchas. The
helper entry points (`iva-volume`, `iva-audio`) are part of the `iva` package,
not in `skills/`.
