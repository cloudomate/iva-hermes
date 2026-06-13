"""`iva-api` — the companion web-console service.

  iva-api serve [host [port]]   # run the HTTP/WS API (default 0.0.0.0:8800)
  iva-api exec '<json>'         # run one JSON-RPC command (incl. chat./skills.*
                                # extras) without HTTP — for testing
  iva-api install               # install + enable the systemd --user unit
"""
import json
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"

    if cmd == "serve":
        import os
        import uvicorn
        from .server import app
        host = argv[1] if len(argv) > 1 else "0.0.0.0"
        port = int(argv[2]) if len(argv) > 2 else int(os.environ.get("IVA_API_PORT", "8800"))
        # HTTPS by default (self-signed; the app pins the fingerprint it gets
        # over BLE via `net.info`). IVA_API_TLS=0 falls back to plain HTTP.
        ssl_kw = {}
        if (os.environ.get("IVA_API_TLS") or "1").strip().lower() not in ("0", "false", "no", "off"):
            from . import tls
            cert, key = tls.ensure()
            ssl_kw = {"ssl_certfile": cert, "ssl_keyfile": key}
        uvicorn.run(app, host=host, port=port, log_level="info", **ssl_kw)
        return 0

    if cmd == "exec":
        from iva.api import actions  # noqa: F401 — extends the REGISTRY
        from iva.ble.rpc import dispatch
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

    print("usage: iva-api {serve [host [port]] | exec '<json>' | install}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
