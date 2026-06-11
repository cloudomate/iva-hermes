"""iva.ble — the companion-app setup service.

A BLE GATT peripheral ("Iva Setup") that lets a phone app configure the device
(WiFi, model endpoints, wake word, audio, Bluetooth) with no SSH and no web UI.

Layered so the risky part is isolated + testable:
  * handlers.py + rpc.py  — transport-agnostic JSON-RPC over the existing iva
    config code (config_cmd / service / display / volume) + nmcli + bluetoothctl.
    Testable on-device with `iva-ble exec '{"action":"status.get"}'` (no BLE).
  * auth.py               — password gate (set on first boot) + bearer tokens.
  * server.py             — the BLE transport (bless GATT peripheral) calling rpc.
  * cli.py / service_setup.py — `iva-ble {serve|exec|install}` + systemd unit.
"""
