"""Shared company name matcher for funding ↔ jobs linkage.

One normalized identity for deciding "is this the same company?" — used by
both the funding scan side and the discovery side, and by the jobs list.

Rules (prompt B):
* lowercase
* strip punctuation / extra whitespace
* drop legal-form suffix tokens only: {inc, inc., ltd, llc, corp, corporation, co, co., plc, gmbh, pvt, pvt., limited, private}
* exact match on the normalized name only — no fuzzy, no substring

A false "funded" badge is worse than a missed one, so no contains/starts_with.
"""
from __future__ import annotations

import re
from typing import Any

_SUFFIX_TOKENS = frozenset({
    "inc", "inc.",
    "ltd",
    "llc",
    "corp", "corporation",
    "co", "co.",
    "plc",
    "gmbh",
    "pvt", "pvt.",
    "limited",
    "private",
})

# After stripping punctuation, "inc." becomes "inc", so we normalise the set
# the same way to keep the comparison consistent.
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_NORMALIZED_SUFFIXES = frozenset(_PUNCT_RE.sub("", t) for t in _SUFFIX_TOKENS)


def normalize_company_name(name: Any) -> str:
    """Normalise a company name for linkage.

    Examples:
        "Acme Inc"            -> "acme"
        "acme, inc."          -> "acme"
        "Acme  Pvt. Ltd."     -> "acme"
        "  Foo-Bar LLC  "      -> "foo bar"
        "" / None             -> ""
    """
    if name is None:
        return ""
    text = str(name).lower().strip()
    if not text:
        return ""
    # Replace any run of non-alphanumeric with a single space, then split.
    # This strips commas, periods, hyphens, ampersands, etc., and collapses
    # whitespace at once.
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in text.split() if t]
    # Drop trailing legal suffix tokens only.
    while tokens and tokens[-1] in _NORMALIZED_SUFFIXES:
        tokens.pop()
    # Also handle the original dotted forms defensively (in case PUNCT_RE
    # changes): check the raw lower tokens as well before punctuation stripping.
    # But after punctuation stripping we already handled it; keep fallback.
    return " ".join(tokens).strip()[:200]
