# Running Iva on a Raspberry Pi / Linux

This is the complete guide to getting **Iva** — the hands-free, wake-word
("Okay Iva") voice assistant — running on a Raspberry Pi or any modern Linux
box with PipeWire.

Iva runs on the device: it listens for the wake word, records you, and
orchestrates the turn. The heavy models (LLM / speech-to-text / text-to-speech)
run on a **backend** reached over the network (local or remote) — Iva just needs
OpenAI-compatible URLs for them.

```
  mic ─▶ wake word ─▶ record ─▶ STT ─▶ Hermes agent (LLM) ─▶ TTS ─▶ speaker
        (on device, PipeWire)         (───── backend over the network ─────)
```

---

## 1. Prerequisites

- **Linux with PipeWire + WirePlumber** running as your user session
  (Raspberry Pi OS Bookworm and most current distros ship this). A user session
  bus is required (`systemctl --user` must work).
- **Python 3.9+**.
- **A microphone and a speaker** — anything PipeWire sees (USB mic, HAT, the
  reSpeaker XVF3800, HDMI/analog out, etc.).
- **Backend endpoints** for LLM, STT and TTS (OpenAI-compatible). See §4.
- Outbound network access to your backend.

### System packages

`sounddevice`/`soundfile` need PortAudio + libsndfile; mp3 TTS playback needs
`ffmpeg`; `iva volume` uses `wpctl` (from WirePlumber). On Debian / Raspberry Pi
OS:

```bash
sudo apt update
sudo apt install -y \
  pipewire pipewire-pulse wireplumber pipewire-alsa \
  libportaudio2 libsndfile1 ffmpeg python3-venv
```

(`pipewire-alsa` lets PortAudio reach PipeWire; `pipewire-pulse` provides
`pactl`/`wpctl` routing.) Verify the audio session is up:

```bash
systemctl --user status pipewire wireplumber   # should be active
wpctl status                                    # lists sinks/sources
```

---

## 2. Install

Use a virtualenv (or the Hermes Agent's venv if you already run one). Greenfield:

```bash
python3 -m venv ~/iva-venv && source ~/iva-venv/bin/activate
pip install iva-hermes
```

This pulls everything Iva needs: `hermes-agent` (the agent runtime) and the audio
stack (`sounddevice`, `soundfile`, and `pymicro-wakeword` — the microWakeWord
runtime). The wake-word models ship **bundled** with `iva-hermes`. Confirm:

```bash
iva --help
iva devices          # should list your sources + sinks
```

> If you prefer, install the Hermes Agent separately (pip or its own shell
> installer) and `pip install iva-hermes` into the **same** environment.

---

## 3. Pick your audio device

Iva is hardware-agnostic over PipeWire. By default it uses the **system default**
mic/speaker with a mono capture — an ordinary USB mic just works.

```bash
iva devices          # find the name/index of your source + sink
iva presets          # list device presets + show the active one
```

### Presets

Known devices have a **preset** that fills in the right capture settings:

| Preset | What it does |
|---|---|
| `generic` (default) | system default source/sink, mono capture |
| `respeaker-xvf3800` | 6-channel capture, extract channel 0 (FL) |

Select one for a run:

```bash
iva run --preset respeaker-xvf3800
```

### Your own defaults — `~/.config/iva-voice/audio.yaml`

```yaml
preset: generic                # which preset to use by default
presets:                        # (optional) define your own device
  my-usb-array:
    source: "USB Audio"         # device name substring, or an index from `iva devices`
    sink: "Headphones"
    channels: 4                 # capture channels (omit for mono)
    wake_channel: 1             # which channel to extract when multi-channel
overrides:                      # (optional) force values regardless of preset
  sink: "bcm2835 Headphones"
```

Environment variables always win over the file and preset (handy in a service
drop-in): `AUDIO_SOURCE`, `AUDIO_SINK`, `AUDIO_CHANNELS`, `WAKE_CH`.

### reSpeaker XVF3800 note

The XVF3800 exposes a 6-channel surround capture; PipeWire's default mono
**downmixes** all 6 and dilutes your voice ~5×, so the wake word never fires.
The `respeaker-xvf3800` preset captures 6ch and extracts FL. You also need to
put the card in the surround profile (find the card with
`pactl list short cards`):

```bash
pactl set-card-profile <your-xvf-card> "output:analog-stereo+input:analog-surround-51"
```

`deploy/ensure-xvf-profile.sh` is an **optional** helper (XVF3800-only) that does
exactly this and restores the saved volume at boot. It's not required — only
useful for that board; if you use one, wire it as the service's `ExecStartPre`.
Other devices don't need it at all.

---

## 4. Configure the backend

Iva needs OpenAI-compatible **LLM, STT and TTS** endpoints (point them at your own
backend — local box or remote). The easy way is `iva config` (no YAML editing):

```bash
iva config set llm.url=http://<backend>:<port>/v1 llm.model=<llm-model>
iva config set stt.url=http://<backend>:<port>/v1 stt.model=<stt-model>
iva config set tts.url=http://<backend>:<port>/v1 tts.model=<tts-model> tts.voice=af_heart
iva config show
```

This writes `~/.hermes/config.yaml`. (Keys: `llm.{url,model,key,provider}`,
`stt.{url,model,key}`, `tts.{url,model,voice,key}`.) The equivalent YAML, if you
prefer to edit it directly:

```yaml
model:
  default: <your-llm-model>
  base_url: http://<backend>:<port>/v1
  api_key: sk-local            # local backends ignore it; the client requires a value
  provider: custom
stt:
  provider: openai
  openai:
    model: <your-stt-model>
    base_url: http://<backend>:<port>/v1
    api_key: sk-local
tts:
  provider: openai
  openai:
    model: <your-tts-model>
    voice: af_heart
    base_url: http://<backend>:<port>/v1
    api_key: sk-local
```

`SOUL.md` (the device-control contract + persona) is injected each turn; the
package ships a default, and the agent reads `~/.hermes/SOUL.md` if present.

---

## 5. Run

```bash
iva run
```

That's the assistant. You should see init lines for the wake word, audio preset,
and backend, then it idles waiting for **"Okay Iva"**. Say it, wait for the beep,
ask something, and Iva replies through the speaker. `Ctrl-C` to stop.

Quick checks while it runs:

```bash
iva volume set 80          # speaker volume (persisted across reboot)
iva audio record 5 /tmp/t.wav && iva audio play /tmp/t.wav   # mic + speaker sanity
```

---

## 6. Run on boot (optional — systemd user service)

To keep Iva running as a background service that starts on boot:

```bash
iva install                # writes ~/.config/systemd/user/hermes-voice.service (ExecStart = iva run)
```

For it to run **without a logged-in session** (headless Pi):

```bash
sudo loginctl enable-linger "$USER"
```

Manage + configure it:

```bash
systemctl --user status hermes-voice
journalctl --user -u hermes-voice -f          # live logs
systemctl --user edit hermes-voice            # add Environment= knobs (see §8), e.g.:
#   [Service]
#   Environment=IVA_AUDIO_PRESET=respeaker-xvf3800
#   Environment=WAKE_CUTOFF=0.7
systemctl --user restart hermes-voice
```

---

## 7. CLI reference

```
iva run [--preset NAME]   run the assistant (the service runs this)
iva config show|set ...   view/set the LLM / STT (ASR) / TTS endpoints
iva doctor                check required deps (Python + system) and how to fix
iva presets               list device presets + the active one
iva devices               list PipeWire sources + sinks
iva volume up|down|set N|get
iva audio record [SECONDS] [FILE] | play FILE | play-last
iva display               optional rich status UI (needs `pip install iva-hermes[display]`)
iva install               install the systemd user service (optional)
```

`iva-volume` and `iva-audio` are also available as bare commands (same as
`iva volume` / `iva audio`).

---

## 8. Tuning knobs (environment)

Set these in the shell for `iva run`, or via `systemctl --user edit hermes-voice`:

| Var | Meaning |
|---|---|
| `IVA_AUDIO_PRESET` | device preset (`generic`, `respeaker-xvf3800`, …) |
| `IVA_WARMUP` | startup LLM warmup: `sync` (default, prime before serving), `async`, or `off` |
| `AUDIO_SOURCE` / `AUDIO_SINK` | pin input/output device (name substring or index) |
| `AUDIO_CHANNELS` | capture channel count (multi-channel arrays) |
| `WAKE_CH` | which channel to extract when multi-channel |
| `WAKE_CUTOFF` | wake sensitivity, 0–1 (lower = more sensitive; default 0.5) |
| `WAKE_DEBUG=1` | log the wake model's peak probability (diagnose misfires) |
| `SPEECH_MULT` / `SPEECH_MIN` | speech-vs-silence threshold tuning |
| `LISTENING_IDLE_TIMEOUT` | seconds to keep listening after a reply before sleeping (default 12) |
| `WAKE_MODELS_DIR` | override where wake-word `*.json` models are loaded from |

Wake-word models ship **bundled** with `iva-hermes` (microWakeWord, scored by the
`pymicro-wakeword` runtime); "Okay Iva" is the active model. To train your own,
see [`iva-wakeword`](https://github.com/cloudomate/iva-wakeword) and copy the
result into the package's `data/wakewords/` (or your `WAKE_MODELS_DIR`).

---

## 9. Troubleshooting

- **Wake word never fires.** Almost always an audio-channel issue. Run with
  `WAKE_DEBUG=1` and watch `peak prob_mean` while you say the wake word — if it
  stays ~0.0, your voice is being downmixed/diluted. For a multi-channel array
  use the matching preset (e.g. `respeaker-xvf3800`) or set `AUDIO_CHANNELS`.
- **No audio / wrong device.** `iva devices` to confirm what PipeWire sees; pin
  `AUDIO_SOURCE`/`AUDIO_SINK`. Make sure `wpctl status` shows the device.
- **"PipeWire/WirePlumber not active".** `systemctl --user status pipewire
  wireplumber`; ensure you have a user session (and `enable-linger` for headless).
- **TTS plays nothing / mp3 error.** Install `ffmpeg` (Iva converts mp3 → wav).
- **`iva volume` says 0% / fails.** Install WirePlumber (`wpctl`).
- **Backend errors / timeouts.** Check the `base_url`s in `~/.hermes/config.yaml`
  are reachable from the device (`curl` them); confirm the backend is up.
- **Logs:** foreground `iva run` prints to the terminal; as a service use
  `journalctl --user -u hermes-voice -f`. Live state is published to
  `$XDG_RUNTIME_DIR/hermes-voice/state.json`.

> **Path note:** some paths still assume the device user `iva` (e.g. the
> conversation-history file `/home/iva/.hermes-voice-history.json`). On the
> reference build the device user is `iva`; on other usernames, set up that path
> or run as `iva` until these are fully parameterised.
