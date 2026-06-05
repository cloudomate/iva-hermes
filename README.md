# iva-hermes

**Iva** — a hands-free, wake-word voice assistant running the Hermes Agent
natively on a Raspberry Pi (reSpeaker XVF3800 mic array + speaker). Wake word
**"Okay Iva"**; heavy models (LLM / STT / TTS) run on a paired backend host.

This repo holds **both** the on-device app and the agent skills:

```
device/                      # the on-device app
  hermes_voice_wake.py       #   headless wake->STT->agent->TTS daemon
  hermes_voice_display.py    #   optional rich status display
  iva-volume                 #   persisted volume helper (used by the skill)
  SOUL.md                    #   device-control contract (auto-injected each turn)
  ensure-xvf-profile.sh      #   boot: XVF3800 6ch profile + restore volume
  hermes-voice.service(.d)   #   systemd user unit + override (WAKE_CUTOFF etc.)
  wakewords/                 #   custom microWakeWord models (.tflite + .json)
  training/                  #   local wake-word training pipeline + TRAINING.md
  README.md                  #   full device setup, backends, gotchas

skills/                      # Hermes Agent skills (consumed as a tap)
  iva-hermes/SKILL.md        #   capabilities umbrella
  volume-control/SKILL.md    #   speaker volume via iva-volume
skills.sh.json               # skills.sh category groupings
install.sh                   # pull skills onto a device + register external_dirs
```

## Skills (tap)

The agent's skills are pulled directly onto devices. Either:

```bash
# native tap (browse/install/update via the Skills Hub):
hermes skills tap add cloudomate/iva-hermes
hermes skills install cloudomate/iva-hermes/skills/volume-control

# or in-place (clone + register in config.yaml skills.external_dirs):
curl -fsSL https://raw.githubusercontent.com/cloudomate/iva-hermes/main/install.sh | bash
```

Skills are **flat** (`skills/<name>/SKILL.md`) so the tap enumerator discovers
them; categories come from `skills.sh.json`. The concrete command contract for
each capability (e.g. `iva-volume up`) is also pinned in `device/SOUL.md`, which
Hermes auto-injects every turn — that's what makes voice device-control reliable.

## Device app

See [`device/README.md`](device/README.md) for the full setup: backends,
PipeWire/XVF3800 config, wake-word training, and the hard-won gotchas. The
helper binaries (e.g. `iva-volume`) ship under `device/`, not in `skills/`.
