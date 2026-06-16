"""Lightweight phishing / scam message detector.

The model is a Naive-Bayes-style linear classifier trained OFFLINE on the
`wangyuancheng/discord-phishing-scam-clean` dataset (1,830 real Discord
messages). Training happens in `dataset/train_phishing_model.py`; the result is
a small JSON of per-token log-odds weights committed at
`dataset/phishing_model.json`.

At runtime there is NO machine-learning dependency: scoring a message is just
tokenising it and summing a handful of float weights from a dict — pure Python,
well under a megabyte of RAM, sub-millisecond per message. That's deliberate so
it runs comfortably inside a 500 MB Railway instance alongside the bot and the
web server.

`tokenize()` is shared by BOTH training and inference so the two never drift.
The training data was cleaned to replace URLs/mentions/emoji/invites with
placeholder tokens (`<URL>`, `<USER>`, `<EMOJI>`, `<DISCORD_INVITE>`); this
tokenizer normalises live Discord messages the same way so a real scam link maps
onto the same features the model learned.
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
import re

log = logging.getLogger("taigabot.phishing")

# Committed model artifact (produced by dataset/train_phishing_model.py).
MODEL_PATH = pathlib.Path(__file__).resolve().parent.parent / "dataset" / "phishing_model.json"

# ── tokeniser (shared by training + inference — keep them identical) ──────────
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.I)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_CHANNEL_RE = re.compile(r"<#\d+>")
_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# A word is letters/digits, OR one of our placeholder tokens (kept whole so the
# angle brackets aren't split apart).
_TOKEN_RE = re.compile(r"<url>|<user>|<emoji>|<discord_invite>|[a-z0-9]+")
# Strip a trailing port and grab the host so a URL contributes its domain words
# (e.g. "steamcommunity", "com") the same way the cleaned dataset kept them.
_HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#\s]+)", re.I)


def _url_replacement(match: re.Match) -> str:
    """Replace a URL with its host words followed by a ``<url>`` marker."""
    host = _HOST_RE.match(match.group(0))
    host_words = host.group(1) if host else ""
    return f" {host_words} <url> "


def tokenize(text: str) -> list[str]:
    """Normalise a message to the dataset's vocabulary and split into tokens.

    Discord-specific syntax (mentions, custom emoji, invite links, URLs) is
    folded into the same placeholder tokens the training data uses, then the
    text is lowercased and split into word/placeholder tokens.
    """
    if not text:
        return []
    t = text.lower()
    # Markdown links: keep the visible label (often a spoofed domain), mark a url.
    t = _MD_LINK_RE.sub(r" \1 <url> ", t)
    # Invite links are their own strong signal.
    t = _INVITE_RE.sub(" <discord_invite> ", t)
    # Remaining URLs: emit the host's words plus a <url> marker.
    t = _URL_RE.sub(_url_replacement, t)
    # Discord entities -> placeholder tokens matching the cleaned dataset.
    t = _MENTION_RE.sub(" <user> ", t)
    t = _CHANNEL_RE.sub(" <user> ", t)
    t = _EMOJI_RE.sub(" <emoji> ", t)
    return _TOKEN_RE.findall(t)


class PhishingModel:
    """A bag-of-tokens log-odds classifier.

    score(text) = bias + Σ weight[token] over tokens present in the message
    (unknown tokens contribute nothing). A score above ``threshold`` is flagged
    as phishing/scam.
    """

    def __init__(self, weights: dict[str, float], bias: float, threshold: float,
                 metadata: dict | None = None):
        self.weights = weights
        self.bias = bias
        self.threshold = threshold
        self.metadata = metadata or {}

    @classmethod
    def load(cls, path: pathlib.Path | str = MODEL_PATH) -> "PhishingModel | None":
        """Load the committed model, or return None if the artifact is missing
        or unreadable (the phishing filter then simply does nothing)."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            log.warning("Phishing model not loaded (%s); filter disabled.", e)
            return None
        return cls(
            weights=data.get("weights", {}),
            bias=float(data.get("bias", 0.0)),
            threshold=float(data.get("threshold", 0.0)),
            metadata={k: v for k, v in data.items() if k not in ("weights",)},
        )

    def score(self, text: str) -> float:
        """Raw log-odds score for a message (higher = more phishing-like)."""
        w = self.weights
        return self.bias + sum(w[tok] for tok in tokenize(text) if tok in w)

    def probability(self, text: str) -> float:
        """The score squashed to a 0–1 phishing probability (for display)."""
        return 1.0 / (1.0 + math.exp(-self.score(text)))

    def is_phishing(self, text: str) -> bool:
        return self.score(text) > self.threshold


# Module-level singleton: load once at import. None if no artifact is present.
MODEL: PhishingModel | None = PhishingModel.load()
