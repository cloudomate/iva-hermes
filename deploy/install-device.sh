#!/usr/bin/env bash
# Install the Iva device app (the `iva` Python package) onto a Raspberry Pi.
# Run this ON the device, from a clone of cloudomate/iva-hermes. Idempotent.
#
# It installs the package into the Hermes venv, exposes the helper entry points
# at the hardcoded paths that SOUL.md / the skills reference, deploys the
# wake-word models + the systemd user unit, and (re)starts the service.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV="${HERMES_VENV:-$HERMES_HOME/hermes-agent/venv}"
PY="$VENV/bin/python"
BIN="$HOME/.local/bin"
WAKE_DIR="${WAKE_MODELS_DIR:-$HOME/wakewords}"
UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "[iva] installing package into Hermes venv: $VENV"
[ -x "$PY" ] || { echo "[iva] ERROR: Hermes venv python not found at $PY"; exit 1; }
"$PY" -m pip install --upgrade "$REPO_ROOT"            # installs the `iva` package + console scripts

echo "[iva] symlinking entry points into $BIN (paths SOUL.md/skills expect)"
mkdir -p "$BIN"
for tool in iva-volume iva-audio; do
  ln -sf "$VENV/bin/$tool" "$BIN/$tool"
done

echo "[iva] deploying bundled wake-word models -> $WAKE_DIR"
# Models ship as package data with iva-hermes; copy them to the device dir.
mkdir -p "$WAKE_DIR"
cp -f "$REPO_ROOT"/src/iva/data/wakewords/* "$WAKE_DIR"/

echo "[iva] deploying helper scripts -> \$HOME"
cp -f "$REPO_ROOT/deploy/ensure-xvf-profile.sh" "$HOME/ensure-xvf-profile.sh"
chmod +x "$HOME/ensure-xvf-profile.sh"

echo "[iva] installing systemd user unit"
mkdir -p "$UNIT_DIR/hermes-voice.service.d"
cp -f "$REPO_ROOT/deploy/hermes-voice.service" "$UNIT_DIR/hermes-voice.service"
cp -f "$REPO_ROOT/deploy/hermes-voice.service.d.override.conf" \
      "$UNIT_DIR/hermes-voice.service.d/override.conf"
systemctl --user daemon-reload
systemctl --user enable --now hermes-voice.service
systemctl --user restart hermes-voice.service

echo "[iva] done. status:"
systemctl --user --no-pager status hermes-voice.service | head -5 || true

# NOTE: SOUL.md (~/.hermes/SOUL.md) is intentionally NOT copied here — the live
# device file can be ahead of the repo (it carries the [[stay]]/[[sleep]] markers
# etc.). Sync it deliberately, diffing first; don't let an installer clobber it.
