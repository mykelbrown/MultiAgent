"""Transparent, rule-based scoring. Scores rank candidates for *human review*; they never auto-approve."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from aegis.discovery.models import Candidate, CandidateKind, Score, ScoredCandidate

WEIGHTS = {
    "relevance": 0.25,
    "security": 0.20,
    "maintenance": 0.15,
    "compatibility": 0.10,
    "license": 0.10,
    "cost": 0.05,
    "permissions": 0.15,
}

PERMISSIVE = {"MIT", "BSD", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "APACHE-2.0", "APACHE", "ISC", "PSF", "MPL-2.0"}
COPYLEFT = {"GPL", "GPL-3.0", "AGPL", "AGPL-3.0", "LGPL", "LGPL-3.0"}
TRUSTED_REPO_HOSTS = ("https://github.com/", "https://gitlab.com/", "https://codeberg.org/")
# Words that suggest write/consequential capabilities in a tool's own description.
WRITE_HINTS = re.compile(r"\b(trade|order|execute|send|delete|write|transfer|payment|withdraw)\w*", re.I)
READ_ONLY_HINTS = re.compile(r"\bread[- ]only\b", re.I)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def score(
    candidate: Candidate, query: str, target_platform: str = "win_amd64", now: datetime | None = None
) -> ScoredCandidate:
    now = now or datetime.now(UTC)
    flags: list[str] = []
    text = f"{candidate.name} {candidate.description}"

    q = _tokens(query)
    relevance = 5.0 * len(q & _tokens(text)) / len(q) if q else 0.0

    security = 1.0
    if candidate.repository_url and candidate.repository_url.startswith(TRUSTED_REPO_HOSTS):
        security += 2.0
    else:
        flags.append("no public source repository: cannot inspect code")
    if candidate.version:
        security += 1.0
    else:
        flags.append("no pinnable version")
    if candidate.is_remote:
        flags.append("remote hosted server: data leaves the machine, code not inspectable")
    else:
        security += 1.0

    if candidate.last_updated is None:
        maintenance = 1.0
        flags.append("unknown last update")
    else:
        age_days = (now - candidate.last_updated).days
        maintenance = 5.0 if age_days <= 90 else 4.0 if age_days <= 180 else 2.5 if age_days <= 365 else 1.0
        if age_days > 365:
            flags.append(f"stale: last update {age_days} days ago")

    compatibility = 3.0
    if candidate.kind is CandidateKind.PYPI_PACKAGE and candidate.platforms:
        universal = any(p.endswith("none-any") or p == "any" for p in candidate.platforms)
        if universal or any(target_platform in p for p in candidate.platforms):
            compatibility = 5.0
        else:
            compatibility = 0.5
            flags.append(f"no wheel for {target_platform}")

    lic = (candidate.license or "").upper().replace(" LICENSE", "").strip()
    if not lic:
        license_score = 1.0
        flags.append("license unknown")
    elif any(lic.startswith(p) for p in PERMISSIVE):
        license_score = 5.0
    elif any(lic.startswith(c) for c in COPYLEFT):
        license_score = 3.0
        flags.append(f"copyleft license {candidate.license}")
    else:
        license_score = 2.0
        flags.append(f"non-standard license {candidate.license!r}")

    cost = 3.0  # unknown by default; pricing must be checked manually

    permissions = 4.0
    if WRITE_HINTS.search(candidate.description) and not READ_ONLY_HINTS.search(candidate.description):
        permissions = 1.5
        flags.append("description implies write/consequential actions")
    elif READ_ONLY_HINTS.search(candidate.description):
        permissions = 5.0

    parts = {
        "relevance": relevance,
        "security": min(security, 5.0),
        "maintenance": maintenance,
        "compatibility": compatibility,
        "license": license_score,
        "cost": cost,
        "permissions": permissions,
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return ScoredCandidate(candidate=candidate, score=Score(**parts, total=round(total, 2), flags=flags))
