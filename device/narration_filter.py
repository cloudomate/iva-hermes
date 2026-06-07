"""Voice-reply narration / filler filter.

Weaker LLMs tend to append sentences like "I'm here for you.", "How can I
help?", "I am listening." after the substantive answer. These narrations
verbalize state metadata the user shouldn't have to hear out loud — the
[[stay]] / [[sleep]] directive already conveys session state silently.

`is_narration(sentence)` returns True if the sentence is pure filler and
should be dropped from TTS. The daemon (`hermes_voice_wake.py`) calls it
inside the streaming sentence segmenter, gated to fire only after a real
content sentence has already played this turn — so a legitimately short
reply like "I'm listening." in response to "are you there?" still gets
through.

Two-stage detection:
  1. Exact-phrase blacklist for unambiguous canonical filler patterns.
  2. Content-word density: tokenize, drop stopwords + filler-state vocab;
     if ZERO content words remain (and the sentence has ≥3 tokens to judge
     by), treat as narration. Catches the long tail without a hardcoded
     phrase per variant.

Tune `NARRATION_PHRASES` / `FILLER_TOKENS` here to extend.

The daemon respects the env var `NARRATION_FILTER` — set to one of
"0","false","no","off" (any case) to bypass the module entirely; sentences
will reach TTS unfiltered.
"""

from __future__ import annotations
import os
import re

# --- phrase blacklist (lowercase, substring match) -------------------------

NARRATION_PHRASES = (
    "listening for your next",
    "feel free to ask",
    "let me know if you",
    "what would you like",
    "going to sleep",
    "see you next time",
    "until next time",
    "talk to you soon",
    "ready when you are",
)

# --- filler-token vocabulary -----------------------------------------------
# A "content word" is any token NOT in this set. Sentences whose tokens are
# all filler/stopwords almost always express state/availability rather than
# real information in a voice-assistant context.

FILLER_TOKENS = frozenset((
    # pronouns / articles / aux verbs
    "i", "im", "i'm", "ive", "i've", "ill", "i'll", "id", "i'd",
    "you", "your", "yours", "me", "my", "mine", "we", "us", "our",
    "a", "an", "the", "this", "that", "these", "those", "it", "its",
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "get", "got",
    "to", "for", "with", "of", "in", "on", "at", "by", "from", "as", "about",
    "and", "or", "but", "so", "if", "when", "what", "how", "why", "then",
    # filler-state predicates and verbs to neutralize
    "here", "there", "ready", "listening", "open", "available", "present",
    "help", "helping", "helps",
    "anything", "something", "anytime", "any", "some", "more", "else", "next",
    "would", "could", "should", "will", "shall", "may", "might", "can",
    "like", "love", "want", "need", "wanted", "needed", "wants", "needs",
    "please", "thank", "thanks", "thankyou", "welcome",
    "now", "always", "still", "just", "really", "very",
    "let", "know", "tell", "ask", "say", "said",
    "feel", "free", "sure",
    "hear", "see", "look", "listen",
    # politeness fragments
    "alright", "okay", "ok", "yes", "no", "yeah", "yep", "nope", "oh", "ah",
    "good", "great", "fine", "sorry",
))

_TOKEN_RE = re.compile(r"[a-zA-Z']+")

# --- enable/disable knob ---------------------------------------------------

def is_enabled() -> bool:
    """Honor NARRATION_FILTER env var. Default ON; off if set to a falsy word."""
    v = (os.environ.get("NARRATION_FILTER") or "").strip().lower()
    return v not in ("0", "false", "no", "off", "disable", "disabled")

# --- main classifier -------------------------------------------------------

def is_narration(sentence: str) -> bool:
    """Return True if `sentence` is pure filler narration that TTS should skip.

    Honors `NARRATION_FILTER` env var: when disabled, always returns False so
    every sentence is treated as real content.
    """
    if not is_enabled(): return False
    s = (sentence or "").lower().strip().rstrip(".!?,")
    if not s: return False
    if any(p in s for p in NARRATION_PHRASES): return True
    tokens = _TOKEN_RE.findall(s)
    if len(tokens) < 3: return False   # too short to judge confidently
    content = [t for t in tokens if t not in FILLER_TOKENS]
    return len(content) == 0
