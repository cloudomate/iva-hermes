#!/bin/bash
# Force the XVF3800 into the 6-channel profile (so the daemon can extract FL)
# and boost playback. Run as ExecStartPre of hermes-voice.service. Best-effort:
# always exits 0 so it never blocks the daemon.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
CARD="alsa_card.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700261100067-00"
PROFILE="output:analog-stereo+input:analog-surround-51"
for i in $(seq 1 25); do
  pactl list short cards 2>/dev/null | grep -q "$CARD" && break
  sleep 1
done
pactl set-card-profile "$CARD" "$PROFILE" 2>/dev/null || true
sleep 1
# Restore the user's last-set volume (persisted by iva-volume); fall back to 1.4.
VOL_STATE="$HOME/.config/iva-voice/volume"
if [ -r "$VOL_STATE" ]; then
  wpctl set-volume @DEFAULT_AUDIO_SINK@ "$(cat "$VOL_STATE")" 2>/dev/null || true
else
  wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.4 2>/dev/null || true
fi
echo "[ensure-xvf] profile=$(pactl list short cards 2>/dev/null | grep -o "analog-surround-[0-9]*" | head -1) vol=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null | awk '{print $2}') done"
exit 0
