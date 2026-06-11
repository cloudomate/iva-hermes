"""`iva-ble` — the companion-app setup service.

  iva-ble serve              # run the BLE GATT peripheral (the real service)
  iva-ble exec '<json>'      # run one JSON-RPC command via the dispatcher (no BLE);
                             # e.g. iva-ble exec '{"action":"status.get"}'   — for testing
  iva-ble install            # install + enable the systemd --user unit
"""
import json
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"

    if cmd == "serve":
        import asyncio
        from .server import serve
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            pass
        return 0

    if cmd == "exec":
        from .rpc import dispatch
        try:
            req = json.loads(argv[1]) if len(argv) > 1 else {}
        except (IndexError, json.JSONDecodeError) as e:
            print(f"exec needs a JSON request: {e}", file=sys.stderr)
            return 2
        print(json.dumps(dispatch(req), indent=2))
        return 0

    if cmd == "install":
        from .service_setup import install
        return install()

    print("usage: iva-ble {serve | exec '<json>' | install}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
