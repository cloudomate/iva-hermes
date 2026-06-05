---
name: iva-hermes
description: "Use to understand what the Iva-Hermes on-device voice assistant is and what hardware it can control. Iva is a hands-free, wake-word ('Okay Iva') voice assistant running natively on a Raspberry Pi with an XVF3800 mic array and a speaker. Lists the device-control capabilities and the specific skills that perform them."
version: 1.0.0
author: Iva / Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [iva, device, voice-assistant, hardware, smart-speaker, capabilities]
    related_skills: [volume-control]
---

# Iva-Hermes — on-device voice assistant

## Overview

**Iva** is a hands-free voice assistant running the Hermes Agent natively on a
Raspberry Pi (with a reSpeaker XVF3800 mic array + speaker). It is woken by the
custom wake word **"Okay Iva"**, transcribes speech, runs the agent, and speaks
the reply. Heavy models (LLM / STT / TTS) run on a paired backend host; the Pi
runs the wake loop, orchestration, and **hardware control**.

This skill is the umbrella description of Iva's on-device capabilities. When a
request maps to one of the capabilities below, load and follow the specific
skill named in the table.

## Device-control capabilities

| Capability | Skill | Helper |
|---|---|---|
| Speaker volume (up/down/set/mute, persisted across reboot) | `volume-control` | `iva-volume` |

_(More device controls — display, brightness, media — get added here as they
ship.)_

## Operating rules

1. **Actually perform device actions** with your tools — never claim you changed
   a setting unless you ran the command and saw it succeed.
2. Prefer the device's **persisted helpers** (e.g. `iva-volume`) over raw
   `wpctl`/`pactl`/`amixer`, so changes survive a reboot.
3. Keep spoken replies short and natural (one or two sentences); this is voice.

## How these skills reach the device

This repo is pulled directly onto the device and exposed to Hermes via
`skills.external_dirs` in `~/.hermes/config.yaml` (see the repo README). The
concrete `iva-volume` command contract is also pinned in the device's
`~/.hermes/SOUL.md` so the agent always has it in context for reliable,
low-latency voice turns.
