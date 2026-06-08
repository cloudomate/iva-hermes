#!/usr/bin/env python3
"""iva-volume — get/adjust/set the default audio sink volume, PERSISTED across
reboots. The persisted level is restored on boot by ensure-xvf-profile.sh
(ExecStartPre of hermes-voice.service), which reads the same state file.

Usage:
  iva-volume up            # +1 step (default 10%)
  iva-volume down          # -1 step
  iva-volume set 50        # set to 50%  (also accepts "50%", or 0.5)
  iva-volume get           # print current level
  iva-volume restore       # re-apply the saved level (used at boot)

Prints "volume NN%" on success. Native wpctl scale (0.0-MAX); MAX>1.0 amplifies
(default MAX 2.50 == 250%, because this speaker is quiet). Do NOT call wpctl/pactl
directly elsewhere — only this helper persists the level so it survives a reboot.

Python port of the original iva-volume bash helper (behaviour-preserving).
"""
import os
import sys
import subprocess

SINK = "@DEFAULT_AUDIO_SINK@"
STATE_DIR = os.path.expanduser("~/.config/iva-voice")
STATE = os.path.join(STATE_DIR, "volume")
STEP = float(os.environ.get("IVA_VOL_STEP", "0.10"))
MAX = float(os.environ.get("IVA_VOL_MAX", "2.50"))


def _wpctl(*args):
    try:
        return subprocess.run(["wpctl", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return None


def cur():
    """Current sink volume as a float (0.0-MAX). wpctl prints e.g. 'Volume: 0.90'."""
    r = _wpctl("get-volume", SINK)
    if not r or r.returncode != 0:
        return 0.0
    for tok in r.stdout.split()[1:]:
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


def clamp(v):
    return round(max(0.0, min(MAX, v)), 2)


def pct(v):
    return int(v * 100 + 0.5)


def apply(v):
    v = clamp(v)
    _wpctl("set-volume", SINK, f"{v:.2f}")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        f.write(f"{v:.2f}\n")
    return v


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.makedirs(STATE_DIR, exist_ok=True)

    cmd = argv[0] if argv else "get"
    arg = argv[1] if len(argv) > 1 else ""

    if cmd == "up":
        v = apply(cur() + STEP)
    elif cmd == "down":
        v = apply(cur() - STEP)
    elif cmd == "set":
        n = float(arg.rstrip("%")) if arg else 0.0
        v = apply(n / 100 if n > 1.5 else n)   # >1.5 means a percent (e.g. 50 -> 0.5)
    elif cmd == "restore":
        if os.path.isfile(STATE):
            saved = open(STATE).read().strip()
            if saved:
                _wpctl("set-volume", SINK, saved)
        v = cur()
    else:  # get (default) or unknown
        v = cur()

    print(f"volume {pct(v)}%")


if __name__ == "__main__":
    main()
