"""Transport-agnostic JSON-RPC dispatch for the setup service.

Request  : {"id": <any>, "action": "<name>", "params": {...}, "token": "<bearer>"}
Response : {"id": <same>, "ok": true, "data": {...}}  |  {"id", "ok": false, "error": "..."}

The BLE server is just a transport that hands raw JSON dicts here. Test without
BLE via `iva-ble exec '{"action": "status.get", "token": "..."}'`.
"""
import traceback

from . import auth, handlers

# actions allowed before/without a session token (bootstrap + onboarding entry)
_OPEN = {"hello", "auth.status", "auth.setup", "auth.login"}


def dispatch(req):
    if not isinstance(req, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    rid = req.get("id")
    action = req.get("action", "")
    params = req.get("params") or {}
    handler = handlers.REGISTRY.get(action)
    if handler is None:
        return {"id": rid, "ok": False, "error": f"unknown action {action!r}"}
    if action not in _OPEN and not auth.valid_token(req.get("token")):
        return {"id": rid, "ok": False, "error": "unauthorized — log in first"}
    try:
        return {"id": rid, "ok": True, "data": handler(params)}
    except handlers.CmdError as e:
        return {"id": rid, "ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface a short error, log the trace
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-500:]}
