"""Extra JSON-RPC actions: chat.history + skills.* + dev.exec

Registered into the SAME registry as the BLE handlers (iva.ble.handlers), so
every transport (HTTP, BLE, `iva-api exec`) exposes one action set. This module
stays dependency-free (stdlib only) — heavy imports happen inside handlers.

Skills model (matches the on-device hermes layout):
  * local skills  — ~/.hermes/skills/<name>/SKILL.md (sometimes <category>/<name>/),
                    created/edited/deleted freely by the user (and this API).
  * external dirs — read-only checkouts listed in config.yaml skills.external_dirs.
  * hub/registry  — searched + installed via the `hermes skills` CLI.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from iva.ble.handlers import REGISTRY, CmdError

LOCAL_SKILLS = os.path.expanduser(os.environ.get("HERMES_SKILLS_DIR", "~/.hermes/skills"))
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Baseline capabilities Iva needs to function — locked ON (the UI shows them
# but won't let you disable them, and the handlers refuse). Disabling e.g. the
# terminal toolset would kill device control; dropping the voice skills would
# break the conversation contract.
_BASELINE_TOOLS = {"terminal", "file", "skills", "memory", "todo", "clarify"}
_BASELINE_SKILLS = {"hermes-agent", "iva-hermes", "iva-voice-interaction",
                    "voice-chat-guidelines", "volume-control", "mic-audio"}


def _hermes_bin():
    cand = os.environ.get("HERMES_BIN") or os.path.join(os.path.dirname(sys.executable), "hermes")
    return cand if os.path.exists(cand) else (shutil.which("hermes") or cand)


def _hermes(args, timeout=120):
    try:
        p = subprocess.run([_hermes_bin(), *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CmdError("hermes CLI not found on this device")
    except subprocess.TimeoutExpired:
        raise CmdError(f"hermes {args[0]} timed out")
    if p.returncode != 0:
        raise CmdError((p.stderr or p.stdout).strip()[-300:] or f"hermes {' '.join(args)} failed")
    return p.stdout


def _external_dirs():
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        return [os.path.expanduser(d) for d in (cfg.get("skills") or {}).get("external_dirs") or []]
    except Exception:
        return []


def _describe(skill_md):
    """First description-ish line from a SKILL.md: frontmatter description, or
    the first non-heading paragraph line."""
    try:
        with open(skill_md, encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(40)]
    except OSError:
        return ""
    for line in head:
        m = re.match(r"^description:\s*(.+)$", line.strip())
        if m:
            return m.group(1).strip().strip('"\'')
    for line in head:
        s = line.strip()
        if s and not s.startswith(("#", "---", "name:", "metadata", "  ")):
            return s[:200]
    return ""


def _walk_skills(root, source):
    """Yield skills under root: <name>/SKILL.md or <category>/<name>/SKILL.md."""
    if not os.path.isdir(root):
        return
    for d1 in sorted(os.listdir(root)):
        p1 = os.path.join(root, d1)
        if not os.path.isdir(p1):
            continue
        if os.path.isfile(os.path.join(p1, "SKILL.md")):
            yield {"name": d1, "category": None, "source": source, "path": p1,
                   "description": _describe(os.path.join(p1, "SKILL.md"))}
            continue
        for d2 in sorted(os.listdir(p1)):
            p2 = os.path.join(p1, d2)
            if os.path.isdir(p2) and os.path.isfile(os.path.join(p2, "SKILL.md")):
                yield {"name": d2, "category": d1, "source": source, "path": p2,
                       "description": _describe(os.path.join(p2, "SKILL.md"))}


def _find(name):
    for root, source in [(LOCAL_SKILLS, "local")] + [(d, "external") for d in _external_dirs()]:
        for s in _walk_skills(root, source):
            if s["name"] == name:
                return s
    return None


def _safe_local(name):
    if not _NAME_RE.match(name or ""):
        raise CmdError("invalid skill name (lowercase letters/digits/.-_ only)")
    path = os.path.realpath(os.path.join(LOCAL_SKILLS, name))
    if not (path + os.sep).startswith(os.path.realpath(LOCAL_SKILLS) + os.sep):
        raise CmdError("invalid skill name")
    return path


def _disabled_skills():
    """The `skills.disabled` list in ~/.hermes/config.yaml — the official
    Hermes mechanism (hermes_cli/skills_config.py reads it at agent start)."""
    from iva import config_cmd as c
    cfg = c._load()
    lst = (cfg.get("skills") or {}).get("disabled") or []
    return cfg, [str(x) for x in lst]


# ------------------------------------------------------------------ handlers
def h_skills_list(_p):
    skills = list(_walk_skills(LOCAL_SKILLS, "local"))
    for d in _external_dirs():
        skills.extend(_walk_skills(d, "external"))
    _, disabled = _disabled_skills()
    for s in skills:
        s["enabled"] = s["name"] not in disabled
        s["locked"] = s["name"] in _BASELINE_SKILLS
    return {"skills": skills, "local_dir": LOCAL_SKILLS, "disabled": disabled}


def h_skills_enable(p):
    return _toggle_skill(p, enable=True)


def h_skills_disable(p):
    return _toggle_skill(p, enable=False)


def _toggle_skill(p, enable):
    from iva import config_cmd as c
    name = (p.get("name") or "").strip()
    if not _find(name):
        raise CmdError(f"no such skill {name!r}")
    if not enable and name in _BASELINE_SKILLS:
        raise CmdError(f"{name!r} is a baseline skill Iva needs — it can't be turned off")
    cfg, disabled = _disabled_skills()
    if enable:
        disabled = [d for d in disabled if d != name]
    elif name not in disabled:
        disabled.append(name)
    c._set(cfg, ("skills", "disabled"), disabled)
    c._save(cfg)
    return {"name": name, "enabled": enable, "restart_needed": True}


# Registries the agent can search/install from (`hermes skills search --source`).
_SKILL_SOURCES = ("all", "official", "skills-sh", "well-known", "github",
                  "clawhub", "lobehub", "browse-sh")


def h_skills_sources(_p):
    """The registries available for search, for the UI's source picker."""
    return {"sources": list(_SKILL_SOURCES)}


def h_skills_search(p):
    q = (p.get("query") or "").strip()
    if not q:
        raise CmdError("query required")
    args = ["skills", "search", "--json", "--limit", str(int(p.get("limit", 20)))]
    source = (p.get("source") or "").strip()
    if source and source != "all":
        if source not in _SKILL_SOURCES:
            raise CmdError(f"unknown source {source!r}")
        args += ["--source", source]
    out = _hermes(args + [q], timeout=90)
    try:
        return {"results": json.loads(out)}
    except json.JSONDecodeError:
        # tolerate leading log lines before the JSON payload
        m = re.search(r"[\[{].*", out, re.S)
        if not m:
            raise CmdError("unexpected search output from hermes")
        return {"results": json.loads(m.group(0))}


def h_skills_install(p):
    # `hermes skills install` exits 0 even when it no-ops: "already installed",
    # or BLOCKED by the security scan (community skill + caution verdict needs
    # --force). Parse the real outcome so the UI doesn't falsely claim success.
    ident = (p.get("id") or p.get("name") or "").strip()
    if not ident:
        raise CmdError("skill id required")
    args = ["skills", "install", ident, "--yes"]
    if p.get("force"):
        args.append("--force")
    out = _hermes(args, timeout=300)
    low = out.lower()
    already = "already installed" in low or "already is installed" in low
    # the real security block (not the "use --force to reinstall" on an
    # already-installed skill, which is harmless)
    blocked = (not already and not p.get("force")
               and ("installation blocked" in low or "decision: blocked" in low))
    installed = not blocked and not already
    return {"id": ident, "installed": installed, "blocked": blocked,
            "already": already, "output": out.strip()[-1500:],
            "restart_needed": installed}


# ----------------------------------------------- private-repo access (token)
def h_skills_github_token(_p):
    """Whether a GitHub token is configured (for private/rate-limited skill
    search + install). Never returns the token itself."""
    from iva.integrations import _get_env_file
    return {"set": bool(_get_env_file().get("GITHUB_TOKEN"))}


def h_skills_set_github_token(p):
    """Save (or clear, if blank) GITHUB_TOKEN in ~/.hermes/.env. hermes loads
    .env per invocation, so search/install pick it up immediately — no restart."""
    from iva.integrations import _set_env_file
    tok = (p.get("token") or "").strip()
    _set_env_file({"GITHUB_TOKEN": tok or None})
    return {"set": bool(tok)}


# --------------------------------------------------------- skill sources (taps)
def _taps_mgr():
    import sys
    if "/home/iva/.hermes/hermes-agent" not in sys.path:
        sys.path.insert(0, "/home/iva/.hermes/hermes-agent")
    from tools.skills_hub import TapsManager
    return TapsManager()


def h_skills_taps(_p):
    """List configured skill sources (GitHub taps)."""
    try:
        return {"taps": _taps_mgr().list_taps()}
    except Exception as e:  # noqa: BLE001
        raise CmdError(f"could not read taps: {e}")


def h_skills_tap_add(p):
    """Add a GitHub repo as a skill source. `hermes skills tap add` clones it
    into ~/.hermes/external-skills and wires it into skills.external_dirs; the
    skills it lists then show up (source=external) after a restart."""
    repo = (p.get("repo") or "").strip()
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise CmdError("repo must look like 'owner/repo'")
    out = _hermes(["skills", "tap", "add", repo], timeout=180)
    return {"added": repo, "output": out.strip()[-1500:], "restart_needed": True}


def h_skills_tap_remove(p):
    repo = (p.get("repo") or p.get("name") or "").strip()
    if not repo:
        raise CmdError("repo required")
    out = _hermes(["skills", "tap", "remove", repo], timeout=60)
    return {"removed": repo, "output": out.strip()[-500:], "restart_needed": True}


def h_skills_uninstall(p):
    name = (p.get("name") or "").strip()
    if not name:
        raise CmdError("skill name required")
    out = _hermes(["skills", "uninstall", name], timeout=60)
    return {"uninstalled": name, "output": out.strip()[-500:]}


def h_skills_read(p):
    s = _find((p.get("name") or "").strip())
    if not s:
        raise CmdError(f"no such skill {p.get('name')!r}")
    md = os.path.join(s["path"], "SKILL.md")
    with open(md, encoding="utf-8", errors="replace") as f:
        content = f.read()
    files = sorted(os.listdir(s["path"]))
    return {**s, "content": content, "files": files, "editable": s["source"] == "local"}


def h_skills_write(p):
    """Create or update a LOCAL skill's SKILL.md."""
    name = (p.get("name") or "").strip()
    content = p.get("content")
    if not isinstance(content, str) or not content.strip():
        raise CmdError("content required")
    existing = _find(name)
    if existing and existing["source"] != "local":
        raise CmdError(f"{name!r} is {existing['source']} (read-only) — create a local copy under a new name")
    path = _safe_local(name)
    os.makedirs(path, exist_ok=True)
    md = os.path.join(path, "SKILL.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(content)
    return {"saved": md, "created": existing is None}


def h_skills_delete(p):
    name = (p.get("name") or "").strip()
    s = _find(name)
    if not s:
        raise CmdError(f"no such skill {name!r}")
    if name in _BASELINE_SKILLS:
        raise CmdError(f"{name!r} is a baseline skill Iva needs — it can't be deleted")
    if s["source"] != "local":
        raise CmdError(f"{name!r} is {s['source']} — only local skills can be deleted here")
    path = _safe_local(name)
    if not os.path.isdir(path):
        raise CmdError(f"no such local skill {name!r}")
    shutil.rmtree(path)
    return {"deleted": name}


def h_chat_history(p):
    from iva.api import chat
    return {"messages": chat.display_history(limit=int(p.get("limit", 50)))}


def h_chat_send(p):
    """Non-streaming text turn (for the phone app, which talks RPC not WS).
    Optional base64 attachments: image (+ image_mime) and audio (+ audio_format,
    a voice note that gets transcribed). The web console uses WS /chat instead."""
    from iva.api import chat
    try:
        return {"reply": chat.run_turn(
            p.get("text") or "", image=p.get("image"), image_mime=p.get("image_mime"),
            audio=p.get("audio"), audio_format=p.get("audio_format"))}
    except ValueError as e:
        raise CmdError(str(e))


# ------------------------------------------------------- developer terminal
# First token must be allowlisted; systemctl/journalctl additionally require
# --user (no system-manager poking from the web). No shell — shlex + argv.
_DEV_ALLOWED = {"hermes", "iva", "iva-volume", "iva-audio", "iva-spotify",
                "wpctl", "nmcli", "systemctl", "journalctl"}
_DEV_NEED_USER_FLAG = {"systemctl", "journalctl"}


def h_dev_exec(p):
    import shlex
    from iva.ble.handlers import _run
    try:
        argv = shlex.split((p.get("command") or "").strip())
    except ValueError as e:
        raise CmdError(f"parse error: {e}")
    if not argv:
        raise CmdError("empty command")
    prog = argv[0]
    if prog not in _DEV_ALLOWED:
        raise CmdError(f"{prog!r} is not allowed here — one of: "
                       + ", ".join(sorted(_DEV_ALLOWED)))
    if prog in _DEV_NEED_USER_FLAG and "--user" not in argv:
        raise CmdError(f"{prog} is only allowed with --user")
    if prog == "hermes":
        argv[0] = _hermes_bin()
    rc, out, err = _run(argv, timeout=60)
    output = (out or "") + (("\n" + err) if err else "")
    return {"exit": rc, "output": output.strip()[-20000:]}


# --------------------------------------------------------------- toolsets
# `hermes tools list` lines look like:  "  ✓ enabled  web  🔍 Web Search…"
_TOOL_RE = re.compile(r"^\s*([✓✗])\s+(enabled|disabled)\s+(\S+)\s+(.*)$")


def _hermes_lenient(args, timeout=180):
    """Like _hermes but returns (rc, combined_output) instead of raising —
    used for `doctor --fix`, which exits non-zero when issues remain but whose
    output we still want to show."""
    try:
        p = subprocess.run([_hermes_bin(), *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "hermes CLI not found on this device"
    except subprocess.TimeoutExpired:
        return 124, f"hermes {' '.join(args)} timed out"


def h_tools_list(_p):
    out = _hermes(["tools", "list"], timeout=30)
    tools = []
    for line in out.splitlines():
        m = _TOOL_RE.match(line)
        if m:
            name = m.group(3)
            tools.append({"name": name, "enabled": m.group(2) == "enabled",
                          "label": m.group(4).strip(),
                          "locked": name in _BASELINE_TOOLS})
    return {"tools": tools}


def h_tools_enable(p):
    name = (p.get("name") or "").strip()
    if not name:
        raise CmdError("tool name required")
    out = _hermes(["tools", "enable", name], timeout=60)
    # Pull in any add-ons/dependencies the newly-enabled toolset needs.
    fix_rc, fix = _hermes_lenient(["doctor", "--fix"], timeout=240)
    combined = (out + "\n--- installing dependencies (doctor --fix) ---\n" + fix).strip()
    return {"name": name, "enabled": True, "restart_needed": True,
            "deps_ok": fix_rc == 0, "output": combined[-6000:]}


def h_tools_disable(p):
    name = (p.get("name") or "").strip()
    if not name:
        raise CmdError("tool name required")
    if name in _BASELINE_TOOLS:
        raise CmdError(f"{name!r} is a baseline tool Iva needs — it can't be turned off")
    out = _hermes(["tools", "disable", name], timeout=60)
    return {"name": name, "enabled": False, "restart_needed": True,
            "output": out.strip()[-1000:]}


# ---------------------------------------------------- messaging gateway
def h_gateway_status(_p):
    from iva import gateway
    return gateway.status()


def h_gateway_set(p):
    from iva import gateway
    try:
        return gateway.set_platform((p.get("platform") or "").strip(), p.get("values") or {})
    except ValueError as e:
        raise CmdError(str(e))


def h_gateway_disconnect(p):
    from iva import gateway
    try:
        return gateway.disconnect((p.get("platform") or "").strip())
    except ValueError as e:
        raise CmdError(str(e))


def h_gateway_service(p):
    from iva import gateway
    try:
        return gateway.service((p.get("action") or "status").strip())
    except ValueError as e:
        raise CmdError(str(e))


EXTRA = {
    "chat.history": h_chat_history, "chat.send": h_chat_send,
    "skills.list": h_skills_list, "skills.search": h_skills_search,
    "skills.install": h_skills_install, "skills.uninstall": h_skills_uninstall,
    "skills.read": h_skills_read, "skills.write": h_skills_write,
    "skills.delete": h_skills_delete,
    "skills.enable": h_skills_enable, "skills.disable": h_skills_disable,
    "skills.sources": h_skills_sources,
    "skills.github_token": h_skills_github_token,
    "skills.set_github_token": h_skills_set_github_token,
    "skills.taps": h_skills_taps, "skills.tap_add": h_skills_tap_add,
    "skills.tap_remove": h_skills_tap_remove,
    "tools.list": h_tools_list, "tools.enable": h_tools_enable,
    "tools.disable": h_tools_disable,
    "gateway.status": h_gateway_status, "gateway.set": h_gateway_set,
    "gateway.disconnect": h_gateway_disconnect, "gateway.service": h_gateway_service,
    "dev.exec": h_dev_exec,
}
REGISTRY.update(EXTRA)
