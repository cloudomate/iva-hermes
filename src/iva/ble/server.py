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

    await server.start()
    log.info("iva-ble: advertising %r (service %s)", DEVICE_NAME, SVC_UUID)
    print(f"iva-ble: advertising {DEVICE_NAME!r} — connect from the app", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await server.stop()
