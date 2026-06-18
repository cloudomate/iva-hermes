"""`iva` — single command-line entry point for the on-device voice assistant.

  iva run [--preset NAME] run the assistant (wake -> STT -> agent -> TTS)
  iva config show|set     view/set the LLM, STT (ASR) and TTS endpoints
  iva doctor              check required deps (Python + system tools) and how to fix
  iva presets             list device presets (e.g. respeaker-xvf3800) + the active one
  iva devices             list PipeWire sources + sinks (to configure audio)
  iva volume <args>       get/adjust speaker volume   (alias of iva-volume)
  iva audio  <args>       record / play audio         (alias of iva-audio)
  iva display             optional rich state UI
  iva install             optional: systemd --user unit to autostart `iva run` on boot
  iva ble <args>          companion-app setup service over BLE (serve|exec|install)
  iva reset [--yes]       factory-style reset: wipe pairing/app settings/history/logs,
                          restart services, re-open BLE onboarding (keeps ~/.hermes)
  iva reset-button        standalone GPIO reset-button listener (--pin/--hold);
                          normally armed inside iva-ble via IVA_RESET_GPIO

Greenfield:  pip install iva-hermes  &&  iva run
"""
import os
import sys

USAGE = __doc__


def _run(rest):
    # `iva run [--preset NAME]` selects a device preset for this run.
    if rest:
        if rest[0] == "--preset" and len(rest) > 1:
            os.environ["IVA_AUDIO_PRESET"] = rest[1]
        elif rest[0].startswith("--preset="):
            os.environ["IVA_AUDIO_PRESET"] = rest[0].split("=", 1)[1]
    from iva.doctor import preflight
    preflight()                      # fail fast (clear message) if a dep is missing
    # The daemon executes at module top level; run it as __main__ without
    # importing it into this process (which would start it on import).
    import runpy
    runpy.run_module("iva.wake", run_name="__main__")


def _doctor(rest):
    from iva.doctor import doctor
    return doctor(rest)


def _config(rest):
    from iva.config_cmd import config
    return config(rest)


def _presets(rest):
    from iva.audio_config import describe
    d = describe()
    print("hardware profiles:")
    for p in d["profiles"]:
        flags = "".join([" *" if p["active"] else "  ",
                         "[detected]" if p["detected"] else ""])
        print(f"  {flags} {p['name']:<20} {p['label']}")
    cur = next((p for p in d["profiles"] if p["active"]), None)
    f = cur["fields"] if cur else {}
    print(f"\nactive: {d['active']}  (via {d['selected_via']})  "
          f"source={f.get('source')!r} sink={f.get('sink')!r} "
          f"channels={f.get('channels')} wake_ch={f.get('wake_channel')} "
          f"cutoff={f.get('wake_cutoff')} vol_max={f.get('vol_max')}")
    print(f"detected for plugged hardware: {d['detected']}")
    print(f"connected cards: {d['cards']}")
    print("\nselect: iva run --preset <name|auto>  |  IVA_AUDIO_PRESET=<name>  |  "
          "app/web 'audio.select_profile'")


def _display(rest):
    import runpy
    runpy.run_module("iva.display", run_name="__main__")


def _volume(rest):
    from iva.volume import main as volume_main
    volume_main(rest)


def _audio(rest):
    from iva.audio import main as audio_main
    audio_main(rest)


def _devices(rest):
    from iva.devices import print_devices
    print_devices()


def _install(rest):
    from iva.service import install
    install(rest)


def _ble(rest):
    from iva.ble.cli import main as ble_main
    return ble_main(rest)


def _reset(rest):
    from iva.reset import reset
    if "--yes" not in rest:
        ans = input("Factory reset? Wipes password/paired devices, app settings, "
                    "history and logs (keeps ~/.hermes backend config) [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    out = reset(restart=True)
    for p in out["removed"]:
        print("removed:", p)
    print("restarting:", ", ".join(out["restarting"]))
    print("BLE onboarding broadcast will be back once iva-ble restarts.")


def _reset_button(rest):
    from iva.reset_button import main as rb_main
    return rb_main(rest)


_COMMANDS = {
    "run": _run,
    "doctor": _doctor,
    "config": _config,
    "install": _install,
    "ble": _ble,
    "reset": _reset,
    "reset-button": _reset_button,
    "presets": _presets,
    "display": _display,
    "volume": _volume,
    "audio": _audio,
    "devices": _devices,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    cmd, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(cmd)
    if handler is None:
        print(f"iva: unknown command {cmd!r}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return handler(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
