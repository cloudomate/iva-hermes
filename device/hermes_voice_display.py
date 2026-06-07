#!/usr/bin/env python3
"""Optional status display for the headless Hermes voice daemon.
Reads the daemon's state JSON and renders a full-screen panel (rich.Live).
Run on the attached screen's console (or any terminal): it never touches audio.
"""
import os, json, time
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.align import Align
from rich.text import Text
from rich.console import Group

STATE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                          "hermes-voice", "state.json")

COLORS = {"listening":"cyan","wake":"bold yellow","recording":"bold red",
          "transcribing":"magenta","thinking":"bold blue","speaking":"bold green",
          "followup":"yellow","loading":"dim","starting":"dim","offline":"red"}
LABEL = {"wake":"WAKE!","recording":"Listening...",
         "transcribing":"Transcribing...","thinking":"Thinking...","speaking":"Speaking...",
         "followup":"Follow-up - just talk","loading":"loading...","starting":"starting...",
         "offline":"daemon offline"}

def read_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"state":"offline"}

def render(s):
    st = s.get("state","offline"); color = COLORS.get(st,"white")
    wake = s.get("wake_name") or "the wake word"
    label = LABEL.get(st) or (f"Listening for '{wake}'" if st == "listening" else st.upper())
    big = Text(label, style=f"{color}", justify="center")
    big.stylize("bold")
    tbl = Table.grid(expand=True); tbl.add_column(justify="left", overflow="fold")
    if s.get("user"):  tbl.add_row(Text(f"You:    {s['user']}", style="bright_white"))
    if s.get("reply"): tbl.add_row(Text(f"Hermes: {s['reply']}", style="green"))
    foot = Text(f"model {s.get('model','?')}    turns {s.get('turns',0)}    wakes {s.get('wakes',0)}",
                style="dim", justify="center")
    body = Group(Text(""), Align.center(big), Text(""), Text(""), tbl, Text(""), foot)
    return Panel(body, title="Hermes Voice", subtitle=wake.lower(), border_style=color, padding=(1,3))

with Live(render(read_state()), refresh_per_second=8, screen=True) as live:
    while True:
        live.update(render(read_state()))
        time.sleep(0.15)
