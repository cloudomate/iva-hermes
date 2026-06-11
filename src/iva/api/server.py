"""The HTTP/WebSocket transport (FastAPI).

  POST /rpc    — one JSON-RPC request (same envelope as BLE) → response dict.
  WS   /chat   — {"token","text"} in; {"type":"delta"|"done"|"error",...} out,
                 streaming the agent's reply token-by-token.
  GET  /healthz
  /            — the built web console (static SPA), if IVA_WEB_DIST exists.

CORS is wide open by design: actions are gated by the bearer token (same
password as BLE onboarding), not by origin.
"""
import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from iva.api import actions  # noqa: F401 — extends the shared action REGISTRY
from iva.api import chat
from iva.ble import auth, rpc

WEB_DIST = os.path.expanduser(os.environ.get("IVA_WEB_DIST", "~/.local/share/iva-web"))

app = FastAPI(title="iva-api", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "iva-api"}


@app.post("/rpc")
def rpc_endpoint(req: dict):
    # sync def on purpose: FastAPI runs it in the threadpool, and every handler
    # (nmcli, bluetoothctl, hermes CLI, ...) blocks.
    return rpc.dispatch(req)


def _delta_text(d):
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("content") or d.get("text") or ""
    return ""


@app.websocket("/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            msg = await ws.receive_json()
            if not auth.valid_token(msg.get("token")):
                await ws.send_json({"type": "error", "error": "unauthorized — log in first"})
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                await ws.send_json({"type": "error", "error": "empty message"})
                continue
            q: asyncio.Queue = asyncio.Queue()

            def on_delta(d):
                t = _delta_text(d)
                if t:
                    loop.call_soon_threadsafe(q.put_nowait, ("delta", t))

            fut = loop.run_in_executor(None, chat.run_turn, text, on_delta)
            fut.add_done_callback(lambda f: q.put_nowait(("done", f)))
            while True:
                kind, payload = await q.get()
                if kind == "delta":
                    await ws.send_json({"type": "delta", "text": payload})
                    continue
                err = payload.exception()
                if err:
                    await ws.send_json({"type": "error", "error": f"{type(err).__name__}: {err}"})
                else:
                    await ws.send_json({"type": "done", "reply": payload.result()})
                break
    except WebSocketDisconnect:
        pass


if os.path.isdir(WEB_DIST):
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
