"""Speaker-verification gate for captured user audio.

Before sending a freshly-recorded turn wav to Whisper, embed it with ECAPA-TDNN
and compare to a pre-computed enrollment embedding. If the cosine similarity
falls below `SPEAKER_THRESHOLD`, the wav is rejected — Whisper is never called.

Why: when TTS leaks into the mic during the followup window, Whisper
hallucinates plausible-but-fake transcripts ("Okay, okay, good to see")
that trigger another LLM turn → loop. Empirically, ECAPA cosine on those
loopback wavs against the user's enrollment hovers near 0 (or negative),
while real user speech sits at 0.5+. A 0.30 threshold cleanly separates the
two in our benchmark.

Side benefit: blocks other people in the room from triggering the assistant
(family members, guests).

This module is **opt-in**. Honor the `SPEAKER_GATE` env var:
  - SPEAKER_GATE=1  → enable (default OFF for safe rollout)
  - SPEAKER_GATE=0  → disable explicitly

Enrollment file is expected at $SPEAKER_GATE_ENROLLMENT (default
~/.config/iva-voice/enrollment.wav). If missing or unreadable, the gate
self-disables and logs a warning — daemon keeps running unchanged.

The ECAPA model + torch are heavy dependencies. We import lazily so a
daemon with `SPEAKER_GATE=0` doesn't pay the cost.
"""

from __future__ import annotations
import os
import sys
from typing import Optional


def is_enabled() -> bool:
    """Honor SPEAKER_GATE env var. Default OFF for safe rollout."""
    v = (os.environ.get("SPEAKER_GATE") or "").strip().lower()
    return v in ("1", "true", "yes", "on", "enable", "enabled")


def _default_enrollment_path() -> str:
    return os.path.expanduser(
        os.environ.get("SPEAKER_GATE_ENROLLMENT")
        or "~/.config/iva-voice/enrollment.wav"
    )


def _default_threshold() -> float:
    try:
        return float(os.environ.get("SPEAKER_GATE_THRESHOLD", "0.30"))
    except ValueError:
        return 0.30


class Gate:
    """Wraps ECAPA-TDNN for one-shot embedding + comparison.

    Constructing Gate eagerly loads the model and embeds the enrollment.
    Errors at construction propagate — caller (the daemon) is expected to
    catch and fall back to "gate disabled" so daemon stays alive even if
    SpeechBrain isn't installed or the enrollment is bad.
    """

    def __init__(self, enrollment_path: str, threshold: float = 0.30):
        # Lazy imports so a disabled gate never pays the torch import cost.
        import warnings
        warnings.filterwarnings("ignore")
        import torch
        import numpy as np
        import soundfile as sf
        import torchaudio
        from speechbrain.inference.speaker import EncoderClassifier

        self._torch = torch
        self._np = np
        self._sf = sf
        self._resample = torchaudio.transforms.Resample
        self._threshold = threshold

        # Cache the model under XDG_CACHE_HOME or ~/.cache so re-runs are fast.
        cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        savedir = os.path.join(cache_root, "speaker_gate", "ecapa")
        self._spk = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir,
            run_opts={"device": "cpu"},
        )

        self._enrollment_emb = self._embed_file(enrollment_path)

    def _embed_file(self, path: str):
        wav_np, sr = self._sf.read(path)
        if wav_np.ndim > 1:
            wav_np = wav_np.mean(axis=1)
        wav = self._torch.from_numpy(wav_np.astype("float32")).unsqueeze(0)
        if sr != 16000:
            wav = self._resample(sr, 16000)(wav)
        with self._torch.no_grad():
            emb = self._spk.encode_batch(wav).squeeze().cpu().numpy()
        return emb / (self._np.linalg.norm(emb) + 1e-9)

    def score(self, wav_path: str) -> float:
        """Cosine similarity in [-1, 1] between wav and enrollment."""
        e = self._embed_file(wav_path)
        return float(self._np.dot(self._enrollment_emb, e))

    def passes(self, wav_path: str) -> tuple[bool, float]:
        """(accepted, cosine). Accepted = score >= threshold."""
        s = self.score(wav_path)
        return (s >= self._threshold, s)


def maybe_build(log=print) -> Optional[Gate]:
    """Construct the gate iff enabled AND enrollment is present AND deps load.
    Returns None on any failure (logged). Daemon should treat None as 'gate off'."""
    if not is_enabled():
        log("[speaker_gate] disabled (SPEAKER_GATE != 1)")
        return None
    path = _default_enrollment_path()
    if not os.path.isfile(path):
        log(f"[speaker_gate] disabled: enrollment not found at {path}")
        return None
    threshold = _default_threshold()
    try:
        gate = Gate(path, threshold=threshold)
    except Exception as e:
        log(f"[speaker_gate] disabled: init failed ({type(e).__name__}: {e})")
        return None
    log(f"[speaker_gate] enabled (enrollment={path}, threshold={threshold:.2f})")
    return gate
