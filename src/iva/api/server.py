"""The HTTP/WebSocket transport (FastAPI).

  POST /rpc    — one JSON-RPC request (same envelope as BLE) → response dict.
  WS   /chat   — {"token","text"} in; {"type":"delta"|"done"|"error",...} out,
                 streaming the agent's reply token-by-token.
  WS   /term   — the developer terminal: first message {token, cols, rows}
                 spawns the user's login SHELL on a PTY (a real terminal — type
                 hermes, iva-volume, journalctl, anything). Then text frames
                 out = terminal output, {"type":"in","data"} in = keystrokes,
                 {"type":"resize","cols","rows"} = winch. Dev-mode + token
                 gated; on a personal device this is a genuine shell, by design.
  GET  /healthz
  /            — the built web console (static SPA), if IVA_WEB_DIST exists.

CORS is wide open by design: actions are gated by the bearer token (paired-app
login), not by origin.
"""
import asyncio
import os
import sys

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


@app.websocket("/term")
async def term_ws(ws: WebSocket):
    """PTY-backed developer terminal (see module docstring): a real login
    shell. Dev-mode + token gated; on a personal device this is a genuine
    shell, by design (the same trust the paired app already has)."""
    import fcntl
    import pty
    import pwd
    import signal
    import struct
    import termios

    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close()
        return
    if not auth.valid_token(first.get("token")):
        await ws.send_text("unauthorized — log in first\r\n")
        await ws.close()
        return

    shell = (os.environ.get("SHELL")
             or (pwd.getpwuid(os.getuid()).pw_shell if os.getuid() else None)
             or "/bin/bash")
    argv = [shell, "-il"]  # interactive login shell

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["PATH"] = os.pathsep.join([os.path.expanduser("~/.local/bin"),
                                   os.path.dirname(sys.executable),
                                   env.get("PATH", "")])
    pid, fd = pty.fork()
    if pid == 0:  # child: becomes the requested command on the PTY slave
        try:
            os.chdir(os.path.expanduser("~"))
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)

    def _winch(cols, rows):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", max(2, int(rows)), max(2, int(cols)), 0, 0))
        except Exception:
            pass

    _winch(first.get("cols") or 100, first.get("rows") or 30)

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    def _on_readable():
        try:
            data = os.read(fd, 8192)
        except OSError:
            data = b""
        out_q.put_nowait(data)
        if not data:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    loop.add_reader(fd, _on_readable)

    async def _pump_out():
        while True:
            data = await out_q.get()
            if not data:
                break
            await ws.send_text(data.decode("utf-8", "replace"))
        try:
            await ws.send_text("\r\n[process exited]\r\n")
            await ws.close()
        except Exception:
            pass

    sender = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "in":
                os.write(fd, (msg.get("data") or "").encode())
            elif kind == "resize":
                _winch(msg.get("cols") or 100, msg.get("rows") or 30)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        sender.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


if os.path.isdir(WEB_DIST):
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
