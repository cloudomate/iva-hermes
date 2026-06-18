#!/bin/bash
# Best-effort audio prep, run as ExecStartPre of hermes-voice.service. Always
# exits 0 so it never blocks the daemon.
#  * If a reSpeaker XVF3800 is present, force its 6ch profile (legacy array) so
#    the daemon can extract FL. Skipped (no 25s wait) for any other hardware,
#    e.g. a generic USB mic/speaker like the Anker PowerConf S330.
#  * Always restore the user-set volume (persisted by iva-volume).
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
CARD="alsa_card.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700261100067-00"
PROFILE="output:analog-stereo+input:analog-surround-51"
if pactl list short cards 2>/dev/null | grep -q "$CARD"; then
  pactl set-card-profile "$CARD" "$PROFILE" 2>/dev/null || true
  sleep 1
fi
# Restore the user's last-set volume (persisted by iva-volume); fall back to 1.4.
VOL_STATE="$HOME/.config/iva-voice/volume"
if [ -r "$VOL_STATE" ]; then
  wpctl set-volume @DEFAULT_AUDIO_SINK@ "$(cat "$VOL_STATE")" 2>/dev/null || true
else
  wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.4 2>/dev/null || true
fi
echo "[ensure-audio] vol=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null | awk '{print $2}') done"
exit 0
