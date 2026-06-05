# iva-hermes

Hermes Agent **skills** for the **Iva** on-device voice assistant (Raspberry Pi
+ reSpeaker XVF3800, wake word "Okay Iva"). Kept in a standalone repo so devices
can **pull them directly** and stay up to date independently of the device app.

```
skills.sh.json                            # category groupings (skills.sh standard)
skills/
  iva-hermes/SKILL.md                     # umbrella: what Iva is + its capabilities
  volume-control/SKILL.md                 # speaker volume (uses the iva-volume helper)
```

Skills are **flat** under `skills/<name>/SKILL.md` (the `hermes skills tap`
enumerator lists immediate children of the tap path and looks for a `SKILL.md`
directly inside each — nested category dirs are not auto-discovered). Categories
are declared in the root `skills.sh.json` sidecar instead.

## How the device consumes these

Hermes discovers external skills via `skills.external_dirs` in
`~/.hermes/config.yaml`. It scans those directories recursively for `SKILL.md`
files (in addition to `~/.hermes/skills/`).

### Install / update (on the device)

```bash
curl -fsSL https://raw.githubusercontent.com/cloudomate/iva-hermes/main/install.sh | bash
# or, if already cloned:
cd ~/.hermes/external-skills/iva-hermes && git pull
```

`install.sh` clones this repo to `~/.hermes/external-skills/iva-hermes` and adds
`~/.hermes/external-skills/iva-hermes/skills` to `skills.external_dirs` in
`config.yaml` (idempotent). Re-run it (or `git pull`) to update — no restart of
the voice service is needed for skill content; the skill index is re-read.

## Notes

- **Public repo: no secrets.** Skills contain only commands and device paths,
  never API keys or endpoints (those live in the device's `config.yaml`).
- The concrete command contract for a capability (e.g. `iva-volume up`) is also
  pinned in the device's `~/.hermes/SOUL.md`, which Hermes auto-injects every
  turn — this is what makes voice device-control reliable and low-latency. The
  skills here are the documented source of truth; SOUL.md references them.
- Helper binaries (e.g. `iva-volume`) ship with the device app
  (`aivg-devices/deploy/iva-hermes-voice/`), not this repo.
