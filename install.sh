#!/usr/bin/env bash
# Install/update the iva-hermes skills on a device and wire them into Hermes.
# Idempotent: safe to re-run. Updates by git-pull if already cloned.
set -euo pipefail

REPO="${IVA_HERMES_REPO:-https://github.com/cloudomate/iva-hermes.git}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/external-skills/iva-hermes"
SKILLS_DIR="$DEST/skills"
CONFIG="$HERMES_HOME/config.yaml"

echo "[iva-hermes] repo:   $REPO"
echo "[iva-hermes] dest:   $DEST"

mkdir -p "$(dirname "$DEST")"
if [ -d "$DEST/.git" ]; then
  echo "[iva-hermes] updating (git pull)..."
  git -C "$DEST" pull --ff-only
else
  echo "[iva-hermes] cloning..."
  git clone --depth 1 "$REPO" "$DEST"
fi

# Register skills/ in config.yaml -> skills.external_dirs (idempotent).
if [ ! -f "$CONFIG" ]; then
  echo "[iva-hermes] WARNING: $CONFIG not found; add this to skills.external_dirs manually:"
  echo "    $SKILLS_DIR"
  exit 0
fi

python3 - "$CONFIG" "$SKILLS_DIR" <<'PY'
import sys, io
path, skills_dir = sys.argv[1], sys.argv[2]
try:
    import yaml
except Exception:
    print("[iva-hermes] PyYAML unavailable; add to config.yaml manually:", skills_dir); sys.exit(0)
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
skills = cfg.get("skills")
if not isinstance(skills, dict):
    skills = {}; cfg["skills"] = skills
dirs = skills.get("external_dirs")
if dirs is None: dirs = []
if isinstance(dirs, str): dirs = [dirs]
# store as ~-relative if under HOME, so the config is portable
import os
home = os.path.expanduser("~")
entry = skills_dir
if skills_dir.startswith(home):
    entry = "~" + skills_dir[len(home):]
if entry in dirs or skills_dir in dirs:
    print("[iva-hermes] external_dirs already set:", entry)
else:
    dirs.append(entry); skills["external_dirs"] = dirs
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    print("[iva-hermes] added to external_dirs:", entry)
PY

echo "[iva-hermes] done. Skills available:"
find "$SKILLS_DIR" -name SKILL.md -printf '  %P\n' 2>/dev/null || find "$SKILLS_DIR" -name SKILL.md
