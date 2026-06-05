---
name: volume-control
description: "Use when the user asks to change the speaker / system / playback volume — e.g. 'turn it up', 'louder', 'lower the volume', 'set volume to 50 percent', 'mute', 'how loud is it'. Adjusts the device audio output and persists the level across reboots."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [smart-home, audio, volume, speaker, voice]
    related_skills: []
---

# Volume Control (this device)

## Overview

Controls this device's audio output volume through the `iva-volume` helper,
which changes the live volume **and** saves it so it survives a reboot (restored
on boot by the voice service). Always go through `iva-volume` — do NOT call
`wpctl`/`pactl` directly, because those changes would not be persisted.

## When to Use

- User asks to make it louder/quieter, turn it up/down, set a specific level,
  mute/unmute, or asks how loud it currently is.
- Don't use for: changing what's playing, switching audio devices, mic/input
  gain (this is output volume only).

## How To

Run the helper with the terminal tool, then tell the user the new level in one
short spoken sentence. The command prints `volume NN%`.

| User intent | Command |
|---|---|
| louder / turn it up / increase | `/home/iva/.local/bin/iva-volume up` |
| quieter / turn it down / lower | `/home/iva/.local/bin/iva-volume down` |
| set to a level (e.g. 50%) | `/home/iva/.local/bin/iva-volume set 50` |
| mute | `/home/iva/.local/bin/iva-volume set 0` |
| max / full volume | `/home/iva/.local/bin/iva-volume set 150` |
| how loud is it / current volume | `/home/iva/.local/bin/iva-volume get` |

Steps are 10% each; "a bit" / "a little" = one `up`/`down`, "a lot" = run it
2–3 times or `set` a target. The scale is the native sink scale where 100% is
unity and up to 150% amplifies (used because this speaker is quiet).

## Response Style (voice)

Keep the spoken reply to one short line, stating the result — e.g.
"Okay, volume's at 90 percent." or "Muted." Don't read out the command or the
raw output.

## Common Pitfalls

1. Calling `wpctl set-volume` directly — the change won't persist across reboot.
   Always use `iva-volume`.
2. Reading the literal `volume NN%` tool output aloud verbatim — paraphrase it.
3. Treating this as input/mic gain — it's playback output only.

## Verification Checklist

- [ ] Ran `iva-volume <action>` via the terminal tool (not raw wpctl).
- [ ] Confirmed the new level back to the user in one short sentence.
