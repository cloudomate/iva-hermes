"""`iva-ble install` — set up the systemd --user unit for the BLE setup service.

Mirrors `iva.service` (the wake daemon). Runs `iva-ble serve` on boot so the
device is always discoverable by the companion app. BLE advertising + GATT
registration go through BlueZ on the system D-Bus, so the user must be able to
talk to it (usually: be in the `bluetooth` group; some distros also need a polkit
rule). We print that guidance rather than silently failing.
"""
import os
import shutil
import subprocess
import sys

UNIT = "iva-ble.service"

_UNIT_TEMPLATE = """\
[Unit]
Description=Iva companion-app setup service (BLE GATT peripheral)
After=bluetooth.service network.target
Wants=bluetooth.service

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m iva.ble.cli serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args])


def install(_rest=None):
    if not shutil.which("systemctl"):
        print("iva-ble install: systemctl not found — run this on the device (Linux/systemd).",
              file=sys.stderr)
        return 1

    home = os.path.expanduser("~")
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(unit_dir, exist_ok=True)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

    unit_path = os.path.join(unit_dir, UNIT)
    with open(unit_path, "w") as f:
        f.write(_UNIT_TEMPLATE.format(python=sys.executable))
    print(f"[iva-ble] wrote {unit_path}")

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT)
    _systemctl("restart", UNIT)
    print("[iva-ble] service installed. status:")
    _systemctl("--no-pager", "status", UNIT)
    print("\n[iva-ble] if advertising fails, ensure BLE access:\n"
          "      sudo usermod -aG bluetooth $USER   # then re-login\n"
          "      (and that bluetooth.service is running: systemctl status bluetooth)")
    return 0


if __name__ == "__main__":
    sys.exit(install(sys.argv[1:]))
