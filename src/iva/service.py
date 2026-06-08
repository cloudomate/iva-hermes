"""`iva install` — set up the systemd --user service for the wake daemon, with
no repo clone: the unit is generated to point at this installed `iva`, and the
helper entry points are symlinked onto PATH.

Run on the device after `pip install iva-hermes`. Linux/systemd only.
Env knobs are added later via a drop-in (`systemctl --user edit hermes-voice`),
e.g. AUDIO_SOURCE / AUDIO_CHANNELS / WAKE_CH for your mic. See `iva devices`.
"""
import os
import shutil
import subprocess
import sys

UNIT = "hermes-voice.service"

_UNIT_TEMPLATE = """\
[Unit]
Description=Iva headless wake-word voice assistant
After=pipewire.service wireplumber.service

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
Environment=OPENAI_API_KEY=sk-local
ExecStart={python} -m iva.cli run
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args])


def install(rest=None):
    if not shutil.which("systemctl"):
        print("iva install: systemctl not found — this sets up a Linux systemd "
              "--user service (run it on the device).", file=sys.stderr)
        return 1

    home = os.path.expanduser("~")
    bindir = os.path.join(home, ".local", "bin")
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(unit_dir, exist_ok=True)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

    # 1) symlink the console scripts onto PATH (SOUL.md / skills call them by name)
    venv_bin = os.path.dirname(sys.executable)
    for tool in ("iva", "iva-volume", "iva-audio"):
        src = os.path.join(venv_bin, tool)
        if os.path.exists(src):
            link = os.path.join(bindir, tool)
            if os.path.islink(link) or os.path.exists(link):
                os.remove(link)
            os.symlink(src, link)
            print(f"[iva] linked {link} -> {src}")

    # 2) generate + write the unit (ExecStart pins this interpreter)
    unit_path = os.path.join(unit_dir, UNIT)
    with open(unit_path, "w") as f:
        f.write(_UNIT_TEMPLATE.format(python=sys.executable))
    print(f"[iva] wrote {unit_path}")

    # 3) enable + (re)start
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT)
    _systemctl("restart", UNIT)
    print("[iva] service installed. status:")
    _systemctl("--no-pager", "status", UNIT)
    print("\n[iva] configure your mic with a drop-in, e.g.:\n"
          "      systemctl --user edit hermes-voice   # add Environment=AUDIO_CHANNELS=6 etc.\n"
          "      (run `iva devices` to find your source/sink)")
    return 0


if __name__ == "__main__":
    sys.exit(install(sys.argv[1:]))
