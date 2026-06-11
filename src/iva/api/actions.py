"""Extra JSON-RPC actions: chat.history + skills.*

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


# ------------------------------------------------------------------ handlers
def h_skills_list(_p):
    skills = list(_walk_skills(LOCAL_SKILLS, "local"))
    for d in _external_dirs():
        skills.extend(_walk_skills(d, "external"))
    return {"skills": skills, "local_dir": LOCAL_SKILLS}


def h_skills_search(p):
    q = (p.get("query") or "").strip()
    if not q:
        raise CmdError("query required")
    out = _hermes(["skills", "search", "--json", "--limit", str(int(p.get("limit", 20))), q],
                  timeout=60)
    try:
        return {"results": json.loads(out)}
    except json.JSONDecodeError:
        # tolerate leading log lines before the JSON payload
        m = re.search(r"[\[{].*", out, re.S)
        if not m:
            raise CmdError("unexpected search output from hermes")
        return {"results": json.loads(m.group(0))}


def h_skills_install(p):
    ident = (p.get("id") or p.get("name") or "").strip()
    if not ident:
        raise CmdError("skill id required")
    out = _hermes(["skills", "install", ident], timeout=300)
    return {"installed": ident, "output": out.strip()[-1000:]}


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


EXTRA = {
    "chat.history": h_chat_history,
    "skills.list": h_skills_list, "skills.search": h_skills_search,
    "skills.install": h_skills_install, "skills.uninstall": h_skills_uninstall,
    "skills.read": h_skills_read, "skills.write": h_skills_write,
    "skills.delete": h_skills_delete,
}
REGISTRY.update(EXTRA)
