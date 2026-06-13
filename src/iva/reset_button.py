"""Physical factory-reset button (Raspberry Pi GPIO).

Hold the button for IVA_RESET_HOLD_S seconds (default 5) to trigger the same
factory-style reset as `iva reset` / the `device.reset` RPC — wipes pairing +
app settings + history/logs, restarts the services, BLE onboarding broadcast
comes back (see iva/reset.py).

Wiring: button between the configured GPIO (BCM numbering) and GND; the
internal pull-up is enabled, so no external resistor is needed.

Armed two ways:
  * inside the always-running iva-ble service when IVA_RESET_GPIO=<pin> is set
    in its environment (maybe_start_thread below) — no extra unit needed;
  * standalone for testing: `iva reset-button --pin 17 --hold 5`.

Requires gpiozero (preinstalled on Raspberry Pi OS). If it's missing or the
pin can't be claimed (e.g. HAT conflict), arming fails soft with a log line —
never takes the setup service down.
"""
import os
import threading
import time


def _arm(pin, hold_s, log=print):
    from gpiozero import Button  # imported lazily: Pi-only dependency
    btn = Button(pin, pull_up=True, hold_time=hold_s)

    def _on_held():
        log(f"[reset-button] held {hold_s:.0f}s — factory reset", flush=True)
        from iva.reset import reset
        out = reset(restart=True)
        for p in out["removed"]:
            log(f"[reset-button] removed: {p}", flush=True)

    btn.when_held = _on_held
    log(f"[reset-button] armed on GPIO{pin} (hold {hold_s:.0f}s to reset)", flush=True)
    return btn


def maybe_start_thread(log=print):
    """Arm the button iff IVA_RESET_GPIO is set. Returns the Button (kept
    alive by the caller) or None. Never raises."""
    pin = (os.environ.get("IVA_RESET_GPIO") or "").strip()
    if not pin:
        return None
    hold_s = float(os.environ.get("IVA_RESET_HOLD_S", "5"))
    try:
        return _arm(int(pin), hold_s, log=log)
    except Exception as e:  # noqa: BLE001 — missing gpiozero / pin conflict
        log(f"[reset-button] not armed ({e})", flush=True)
        return None


def main(argv=None):
    """`iva reset-button [--pin N] [--hold S]` — standalone listener."""
    import argparse
    ap = argparse.ArgumentParser(prog="iva reset-button")
    ap.add_argument("--pin", type=int, default=int(os.environ.get("IVA_RESET_GPIO", "17")))
    ap.add_argument("--hold", type=float, default=float(os.environ.get("IVA_RESET_HOLD_S", "5")))
    args = ap.parse_args(argv)
    _arm(args.pin, args.hold)
    threading.Event().wait()  # gpiozero fires callbacks; just stay alive
    return 0
