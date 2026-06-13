"""BLE GATT peripheral transport for the setup service.

Advertises an "Iva Setup" service with three characteristics:
  * cmd    (write / write-without-response)  — app writes a JSON request, framed
            as raw bytes terminated by a newline (chunk freely across BLE MTUs).
  * resp   (notify)                          — device notifies the JSON response,
            chunked to MTU-sized writes, newline-terminated.
  * status (notify)                          — device pushes state.json on change.

Everything below the framing is plain rpc.dispatch — see rpc.py / handlers.py.
Dispatch runs in a worker thread so a slow command (nmcli, bluetoothctl) never
stalls the BLE event loop. Requires `pip install iva-hermes[ble]` (bless) and a
working BlueZ stack; validate/tune advertising + MTU on the device.

NOTE: BLE specifics (bless API surface, MTU, advertising perms under BlueZ) are
device-validated — this is the transport skeleton, not yet hardware-proven.
"""
import asyncio
import json
import logging
import subprocess

log = logging.getLogger("iva.ble")

# Custom 128-bit UUIDs (fixed for this product).
SVC_UUID = "6e9a0001-7b8c-4d2e-9f10-1a2b3c4d5e6f"
CMD_UUID = "6e9a0002-7b8c-4d2e-9f10-1a2b3c4d5e6f"
RESP_UUID = "6e9a0003-7b8c-4d2e-9f10-1a2b3c4d5e6f"
STATUS_UUID = "6e9a0004-7b8c-4d2e-9f10-1a2b3c4d5e6f"

DEVICE_NAME = "Iva Setup"
CHUNK = 180  # conservative notify chunk; raised after MTU negotiation later


async def serve():
    from bless import (  # imported lazily so the base package needn't depend on bless
        BlessServer,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
    from .rpc import dispatch

    loop = asyncio.get_running_loop()
    server = BlessServer(name=DEVICE_NAME)
    rx = bytearray()  # reassembly buffer for inbound (cmd) frames

    async def _notify(uuid, payload: bytes):
        char = server.get_characteristic(uuid)
        data = payload + b"\n"
        for i in range(0, len(data), CHUNK):
            char.value = data[i:i + CHUNK]
            server.update_value(SVC_UUID, uuid)
            await asyncio.sleep(0.02)

    async def _handle_frame(frame: bytes):
        try:
            req = json.loads(frame.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"bad JSON: {e}"}
        else:
            # run the (possibly blocking) dispatch off the BLE loop
            resp = await loop.run_in_executor(None, dispatch, req)
        await _notify(RESP_UUID, json.dumps(resp).encode("utf-8"))

    def _on_write(characteristic, value, **_kw):
        # bless write callback (sync); buffer + schedule frame handling on the loop
        if str(characteristic.uuid).lower() != CMD_UUID:
            return
        rx.extend(bytes(value))
        while b"\n" in rx:
            frame, _, rest = rx.partition(b"\n")
            del rx[:]
            rx.extend(rest)
            if frame.strip():
                asyncio.run_coroutine_threadsafe(_handle_frame(bytes(frame)), loop)

    def _on_read(characteristic, **_kw):
        return characteristic.value

    server.write_request_func = _on_write
    server.read_request_func = _on_read

    await server.add_new_service(SVC_UUID)
    wprops = (GATTCharacteristicProperties.write
              | GATTCharacteristicProperties.write_without_response)
    nprops = GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read
    await server.add_new_characteristic(
        SVC_UUID, CMD_UUID, wprops, None, GATTAttributePermissions.writeable)
    await server.add_new_characteristic(
        SVC_UUID, RESP_UUID, nprops, b"", GATTAttributePermissions.readable)
    await server.add_new_characteristic(
        SVC_UUID, STATUS_UUID, nprops, b"", GATTAttributePermissions.readable)

    # Physical factory-reset button (no-op unless IVA_RESET_GPIO is set).
    # Kept referenced so gpiozero's callbacks stay alive for the process.
    from iva.reset_button import maybe_start_thread
    _reset_btn = maybe_start_thread()  # noqa: F841

    # --- pairing-aware advertising gate ---------------------------------
    # Out-of-box (no password yet): advertise so the app can onboard.
    # Paired: stop the broadcast — strangers in BLE range see nothing; the
    #   app talks over the encrypted LAN API (it knows host + pin + secret).
    # Rescue: if the device loses its network, the paired phone can't reach
    #   the LAN API either — resume advertising so BLE can fix the Wi-Fi.
    # A factory reset (`iva reset` / device.reset / GPIO button) clears the
    # pairing, so the gate re-opens within one poll interval.
    def _lan_ok():
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=5).stdout
            return bool(out.strip())
        except Exception:
            return False

    def _should_advertise():
        # Advertise while unpaired (out-of-box onboarding), while a pairing
        # window is open (share access), or while offline (BLE rescue).
        from . import auth
        return (not auth.has_paired_devices()) or auth.pair_window_open() or (not _lan_ok())

    async def _is_connected():
        # bless API variance: sync or async depending on backend/version.
        try:
            r = server.is_connected()
            return (await r) if asyncio.iscoroutine(r) else bool(r)
        except Exception:
            return False

    advertising = _should_advertise()
    if advertising:
        await server.start()
        log.info("iva-ble: advertising %r (service %s)", DEVICE_NAME, SVC_UUID)
        print(f"iva-ble: advertising {DEVICE_NAME!r} — connect from the app", flush=True)
    else:
        print("iva-ble: paired + online — BLE broadcast off (app uses the LAN API; "
              "broadcast resumes if the network drops or after a reset)", flush=True)
    try:
        while True:
            await asyncio.sleep(5)  # short poll so a pairing window airs promptly
            want = _should_advertise()
            if want and not advertising:
                await server.start()
                advertising = True
                print("iva-ble: broadcast resumed (unpaired or network down)", flush=True)
            elif not want and advertising and not await _is_connected():
                # Don't cut an in-flight BLE session; stop once idle.
                await server.stop()
                advertising = False
                print("iva-ble: paired + online — broadcast stopped", flush=True)
    finally:
        if advertising:
            await server.stop()
