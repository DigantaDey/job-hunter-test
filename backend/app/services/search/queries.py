"""Fail-closed minimization: queries use ONLY curated public role vocabulary.

Free-form titles are never echoed, even when they contain a name rather than an
obvious email/phone number. Unsupported preferences are omitted, not sent raw.
Locations, employers, evidence, resume text, skills and identity are not inputs.
"""
from dataclasses import dataclass
from typing import Any, Mapping

# Deliberately conservative; extend this public taxonomy to support more roles.
ROLES = frozenset({
    "software engineer", "software developer", "backend engineer", "backend developer",
    "frontend engineer", "frontend developer", "full stack engineer", "full stack developer",
    "data engineer", "data scientist", "data analyst", "machine learning engineer",
    "devops engineer", "site reliability engineer", "platform engineer", "cloud engineer",
    "security engineer", "qa engineer", "test engineer", "mobile engineer", "android developer",
    "ios developer", "product manager", "project manager", "program manager", "engineering manager",
    "product designer", "ux designer", "ui designer", "business analyst", "financial analyst",
    "accountant", "sales engineer", "account executive", "marketing manager", "operations manager",
    "customer success manager", "technical writer", "recruiter", "nurse", "teacher",
})
LEVELS = ("junior", "senior", "staff", "principal", "lead")


@dataclass(frozen=True)
class SearchPreferences:
    roles: tuple[str, ...] = ()
    remote: bool = False

    @classmethod
    def from_normalized(cls, preferences: Mapping[str, Any]) -> "SearchPreferences":
        raw = preferences.get("target_roles", [])
        if not isinstance(raw, list):
            return cls()
        roles = set()
        for value in raw[:20]:
            if not isinstance(value, str) or len(value) > 80:
                continue
            role = " ".join(value.lower().replace("-", " ").split())
            base = role
            for level in LEVELS:
                if role.startswith(level + " "):
                    base = role[len(level) + 1:]
                    break
            if base in ROLES:
                roles.add(role)
        return cls(tuple(sorted(roles)[:3]), preferences.get("remote") == "remote")


def generate_queries(preferences: SearchPreferences) -> list[str]:
    # Re-validate even manually constructed values at the provider boundary.
    safe = SearchPreferences.from_normalized({"target_roles": list(preferences.roles),
                                             "remote": "remote" if preferences.remote else ""})
    queries = []
    for role in safe.roles:
        stem = f'"{role}"' + (" remote" if safe.remote else "")
        queries.extend([
            f'{stem} careers jobs',
            f'{stem} (site:jobs.lever.co OR site:boards.greenhouse.io OR site:jobs.ashbyhq.com)',
            f'{stem} "job posting" apply',
        ])
    # Round-robin roles so a small budget doesn't only search the first role.
    return [queries[i] for offset in range(3) for i in range(offset, len(queries), 3)]
