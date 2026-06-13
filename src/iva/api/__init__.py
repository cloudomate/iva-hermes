"""iva.api — the companion web-console service (HTTP + WebSocket).

A second transport over the SAME JSON-RPC layer as iva.ble (rpc.dispatch +
auth + handlers): the phone app uses BLE for out-of-the-box onboarding; the
web console uses this API on the LAN for everything after — status/config,
streaming text chat with the SAME agent (and conversation) the voice daemon
uses, and skills management.

Layout (mirrors iva.ble):
  * actions.py       — transport-agnostic extra handlers (chat.history,
                       skills.*) registered into the shared iva.ble REGISTRY.
                       Dependency-free: also usable over BLE / `iva-api exec`.
  * chat.py          — text turns through AIAgent, sharing the voice daemon's
                       history file (one conversation across voice and web).
  * server.py        — the FastAPI transport (POST /rpc, WS /chat, static SPA).
  * cli.py / service_setup.py — `iva-api {serve|exec|install}` + systemd unit.

Tokens are per-process (see iva.ble.auth): log in over the transport you use.
"""
