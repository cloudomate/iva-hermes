# iva-hermes

**Iva** — a hands-free, wake-word voice assistant running the Hermes Agent
natively on a Raspberry Pi (reSpeaker XVF3800 mic array + speaker). Wake word
**"Okay Iva"**; heavy models (LLM / STT / TTS) run on a paired backend host.

This repo holds the **on-device app**. The agent skills live in a dedicated
repo — [`cloudomate/skills`](https://github.com/cloudomate/skills):

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

install.sh                   # pull cloudomate/skills onto a device + register external_dirs
```

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
`device/SOUL.md` here, which Hermes auto-injects every turn — that redundancy is
what makes voice device-control reliable.

## Device app

See [`device/README.md`](device/README.md) for the full setup: backends,
PipeWire/XVF3800 config, wake-word training, and the hard-won gotchas. The
helper binaries (e.g. `iva-volume`) ship under `device/`, not in `skills/`.

## Notes

- **Public repo: no secrets.** Endpoints + API keys live in the device's
  `~/.hermes/config.yaml`, never here.
- The device app moved here from `aivg-devices/deploy/iva-hermes-voice/`; the
  agent skills then moved out to [`cloudomate/skills`](https://github.com/cloudomate/skills).
