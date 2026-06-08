"""Iva — the on-device, wake-word ("Okay Iva") voice assistant that runs the
Hermes Agent natively on a Raspberry Pi (reSpeaker XVF3800 mic array + speaker).

Modules:
  wake             headless wake-word daemon (run: ``python -m iva.wake``)
  display          optional rich state UI (run: ``python -m iva.display``)
  audio            mic record / playback helper (entry point: ``iva-audio``)
  volume           persisted speaker-volume control (entry point: ``iva-volume``)
  narration_filter / speaker_gate   helpers used by the wake daemon

Wake-word models are bundled in iva/data/wakewords (microWakeWord; the
pymicro-wakeword runtime scores them). Training lives in cloudomate/iva-wakeword.
"""

__version__ = "0.1.0"
