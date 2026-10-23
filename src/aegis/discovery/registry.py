"""Tool registry with an explicit lifecycle and rollback path.

discovered -> quarantined -> approved (owner approval, exact version pinned)
approved   -> rolled_back (restores the previously approved version, if any)
Registering a tool here never installs it; installation is a separate, manual, reviewed step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from aegis.core.audit import AuditLog
from aegis.core.permissions import ApprovalStore, Capability, PermissionDenied, PermissionGate
from aegis.discovery.models import ScoredCandidate, ToolStatus
from aegis.discovery.scoring import score
from aegis.discovery.sources import Source

_TRANSITIONS = {
    ToolStatus.DISCOVERED: {ToolStatus.QUARANTINED, ToolStatus.REJECTED},
    ToolStatus.QUARANTINED: {ToolStatus.APPROVED, ToolStatus.REJECTED},
    ToolStatus.APPROVED: {ToolStatus.ROLLED_BACK},
    ToolStatus.REJECTED: set(),
    ToolStatus.ROLLED_BACK: {ToolStatus.QUARANTINED},
}


class RegistryEntry(BaseModel):
    key: str  # kind:name
    status: ToolStatus
    pinned_version: str | None
    scored: ScoredCandidate
    history: list[dict[str, str | None]] = []  # status/version changes, newest last
    notes: str = ""


class ToolRegistry:
    def __init__(self, path: Path | None, audit: AuditLog, approvals: ApprovalStore) -> None:
        self._path = path
        self._audit = audit
        self._approvals = approvals
        self._entries: dict[str, RegistryEntry] = {}
        if path and path.exists():
            for raw in json.loads(path.read_text()):
                e = RegistryEntry.model_validate(raw)
                self._entries[e.key] = e

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([e.model_dump(mode="json") for e in self._entries.values()], indent=2))

    def get(self, key: str) -> RegistryEntry | None:
        return self._entries.get(key)

    def entries(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def record(self, scored: ScoredCandidate) -> RegistryEntry:
        key = f"{scored.candidate.kind}:{scored.candidate.name}"
        existing = self._entries.get(key)
        if existing:
            existing.scored = scored  # refresh metadata; status/pin unchanged
            entry = existing
        else:
            entry = RegistryEntry(key=key, status=ToolStatus.DISCOVERED, pinned_version=None, scored=scored)
            self._entries[key] = entry
        self._save()
        return entry

    def transition(self, key: str, to: ToolStatus, actor: str, notes: str = "") -> RegistryEntry:
        entry = self._entries[key]
        if to not in _TRANSITIONS[entry.status]:
            raise ValueError(f"illegal transition {entry.status} -> {to}")
        if to is ToolStatus.APPROVED:
            version = entry.scored.candidate.version
            if not version:
                raise ValueError("cannot approve a tool without a pinnable version")
            if self._approvals.consume(Capability.INSTALL_TOOL, {"tool": key}) is None:
                self._audit.append(actor, "tool.approve_denied", {"tool": key})
                raise PermissionDenied(f"approving {key} requires an owner approval scoped to it")
            entry.pinned_version = version
        if to is ToolStatus.ROLLED_BACK:
            previous = [h["version"] for h in entry.history if h["status"] == ToolStatus.APPROVED]
            entry.pinned_version = previous[-2] if len(previous) >= 2 else None
        entry.status = to
        entry.notes = notes or entry.notes
        entry.history.append(
            {"status": to.value, "version": entry.pinned_version, "at": datetime.now(UTC).isoformat(), "by": actor}
        )
        self._save()
        self._audit.append(actor, "tool.transition", {"tool": key, "to": to, "version": entry.pinned_version})
        return entry


class DiscoveryService:
    AGENT = "discovery"

    def __init__(self, sources: list[Source], registry: ToolRegistry, gate: PermissionGate, audit: AuditLog) -> None:
        self._sources = sources
        self._registry = registry
        self._gate = gate
        self._audit = audit

    def discover(self, query: str, limit: int = 10, platform: str = "win_amd64") -> list[ScoredCandidate]:
        self._gate.check(self.AGENT, Capability.DISCOVERY_READ)
        results: list[ScoredCandidate] = []
        for source in self._sources:
            try:
                found = source.search(query, limit)
            except Exception as exc:  # noqa: BLE001 - one bad source must not kill discovery
                self._audit.append(self.AGENT, "discovery.source_error", {"source": source.name, "error": str(exc)})
                continue
            for cand in found:
                scored = score(cand, query, platform)
                self._registry.record(scored)
                results.append(scored)
        results.sort(key=lambda s: s.score.total, reverse=True)
        self._audit.append(
            self.AGENT,
            "discovery.search",
            {"query": query, "results": [(s.candidate.name, s.score.total) for s in results]},
        )
        return results
