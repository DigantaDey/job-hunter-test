"""Role-family normalisation — one function, every report that groups by role.

The analytics page used to carry this heuristic inline. The moment a second
caller needed "group applications by role" (the tracking report does), keeping
it inline meant two implementations of the same grouping key, and two
implementations of a grouping key is how a dashboard ends up contradicting
itself: "Backend Engineer: 12 applied" on one page, 9 on the other, both
*self-consistent*.

So the mapping lives here, is pure, and is what both readers call. It is
deliberately coarse — a keyword pass over the title, not a classifier — and it
falls back to the raw title, because a report that silently buckets an
unrecognised title into "Other" hides exactly the roles the user is applying to
most.
"""
from __future__ import annotations

from typing import Tuple

__all__ = ["ROLE_FAMILIES", "role_family", "normalize_role_key"]

#: Ordered (family, keywords) rules. First match wins, so the order *is* the
#: precedence: "Full Stack Data Engineer" is Full Stack, not Data.
ROLE_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Backend Engineer", ("backend", "back-end", "back end")),
    ("Frontend Engineer", ("frontend", "front-end", "front end")),
    ("Full Stack", ("full stack", "full-stack", "fullstack")),
    ("Data Engineer", ("data engineer", "data science", "data scientist", "data analyst", "analytics engineer", "data")),
    ("ML Engineer", ("machine learning", "ml engineer", "mlops", " ml")),
    ("DevOps/SRE", ("devops", "dev-ops", "site reliability", "sre", "platform engineer", "infrastructure")),
    ("Product Manager", ("product manager", "product owner", "product")),
    ("Engineering Manager", ("engineering manager", "tech lead", "team lead", "head of engineering")),
    ("QA Engineer", ("qa engineer", "quality engineer", "test engineer", "sdet", "qa")),
    ("Mobile Engineer", ("android", "ios", "mobile")),
    ("Security Engineer", ("security engineer", "appsec", "cyber security", "information security")),
    ("Embedded Engineer", ("embedded", "firmware")),
    ("Designer", ("designer", "ux", "ui ", "product design")),
)


def role_family(title: str | None) -> str:
    """The reporting family for a job title (the raw title when nothing matches).

    ``full stack`` needs the ``full`` *and* ``stack`` check the shipped analytics
    heuristic used, so a title like "Full-Stack Developer" and "Fullstack
    Engineer" both land in one bucket; the keyword list carries both spellings.
    """
    text = (title or "").strip()
    if not text:
        return "Other"
    lowered = f" {text.lower()} "
    for family, keywords in ROLE_FAMILIES:
        for keyword in keywords:
            if keyword in lowered:
                return family
    # Unrecognised: keep the title so the group is still meaningful, rather than
    # collapsing every unusual role into one undifferentiated "Other".
    return text[:80]


def normalize_role_key(value: str | None) -> str:
    """Stable grouping key for a role family (case/punctuation-insensitive)."""
    text = (value or "").strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_") or "unknown"
