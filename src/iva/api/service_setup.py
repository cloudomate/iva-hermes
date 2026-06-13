"""`iva-api install` — set up the systemd --user unit for the web-console API.

Mirrors iva-ble's service_setup. Runs `iva-api serve` on boot so the web
console is reachable whenever the device is on the network.
"""
import os
import shutil
import subprocess
import sys

UNIT = "iva-api.service"

_UNIT_TEMPLATE = """\
[Unit]
Description=Iva web-console API (HTTP + WebSocket)
After=network.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m iva.api.cli serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args])


def install(_rest=None):
    if not shutil.which("systemctl"):
        print("iva-api install: systemctl not found — run this on the device (Linux/systemd).",
              file=sys.stderr)
        return 1

    unit_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    os.makedirs(unit_dir, exist_ok=True)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

    unit_path = os.path.join(unit_dir, UNIT)
    with open(unit_path, "w") as f:
        f.write(_UNIT_TEMPLATE.format(python=sys.executable))
    print(f"[iva-api] wrote {unit_path}")

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT)
    _systemctl("restart", UNIT)
    print("[iva-api] service installed. status:")
    _systemctl("--no-pager", "status", UNIT)
    return 0


if __name__ == "__main__":
    sys.exit(install(sys.argv[1:]))
