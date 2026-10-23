"""Deny-by-default capability gate and human-approval store.

Agents never hold policy write access: the per-agent allowlist is loaded once at start-up
and the gate only answers yes/no. Consequential capabilities additionally require a
single-use, expiring approval that only the owner can create (via the CLI / dashboard).
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from aegis.core.audit import AuditLog


class Capability(StrEnum):
    # Trading
    PLACE_PAPER_ORDER = "trading.place_paper_order"
    PLACE_DEMO_ORDER = "trading.place_demo_order"
    PLACE_LIVE_ORDER = "trading.place_live_order"
    ENABLE_LIVE_TRADING = "trading.enable_live"
    INCREASE_RISK_LIMITS = "trading.increase_risk_limits"
    RESET_KILL_SWITCH = "trading.reset_kill_switch"
    TRIP_KILL_SWITCH = "trading.trip_kill_switch"
    READ_MARKET_DATA = "trading.read_market_data"
    # Messaging
    READ_TELEGRAM_SIGNALS = "telegram.read_signals"
    # Engineering
    WRITE_AGENT_BRANCH = "repo.write_agent_branch"
    MERGE_TO_MAIN = "repo.merge_main"
    # Discovery
    DISCOVERY_READ = "discovery.read"
    INSTALL_TOOL = "discovery.install_tool"
    # Assistant
    READ_ALLOWLISTED_FILES = "assistant.read_allowlisted_files"
    DRAFT_EXTERNAL_MESSAGE = "assistant.draft_message"
    SEND_EXTERNAL_MESSAGE = "assistant.send_message"
    MOVE_FUNDS = "finance.move_funds"
    # Evaluation
    RUN_EVALS = "evals.run"


# Capabilities that require an owner approval each time, even when allowlisted.
APPROVAL_REQUIRED: frozenset[Capability] = frozenset(
    {
        Capability.PLACE_LIVE_ORDER,
        Capability.ENABLE_LIVE_TRADING,
        Capability.INCREASE_RISK_LIMITS,
        Capability.RESET_KILL_SWITCH,
        Capability.MERGE_TO_MAIN,
        Capability.INSTALL_TOOL,
        Capability.SEND_EXTERNAL_MESSAGE,
        Capability.MOVE_FUNDS,
    }
)

# Default least-privilege allowlists. Nothing grants MOVE_FUNDS or MERGE_TO_MAIN to an agent.
DEFAULT_AGENT_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "forex_research": frozenset(
        {Capability.READ_MARKET_DATA, Capability.PLACE_PAPER_ORDER, Capability.TRIP_KILL_SWITCH}
    ),
    "telegram_intake": frozenset(
        {
            Capability.READ_TELEGRAM_SIGNALS,
            Capability.READ_MARKET_DATA,
            Capability.PLACE_PAPER_ORDER,
            Capability.PLACE_DEMO_ORDER,
            Capability.TRIP_KILL_SWITCH,
        }
    ),
    "prompt_eval": frozenset({Capability.RUN_EVALS}),
    "agent_factory": frozenset({Capability.WRITE_AGENT_BRANCH, Capability.RUN_EVALS}),
    "assistant": frozenset(
        {
            Capability.READ_ALLOWLISTED_FILES,
            Capability.DRAFT_EXTERNAL_MESSAGE,
            Capability.SEND_EXTERNAL_MESSAGE,
        }
    ),
    "discovery": frozenset({Capability.DISCOVERY_READ}),
    "reconciler": frozenset({Capability.TRIP_KILL_SWITCH, Capability.READ_MARKET_DATA}),
}


class PermissionDenied(Exception):
    pass


@dataclass
class Approval:
    id: str
    capability: Capability
    scope: dict[str, Any]
    approved_by: str
    created_at: datetime
    expires_at: datetime
    consumed: bool = False


class ApprovalStore:
    """Owner-created, single-use, expiring approvals, persisted as JSON."""

    def __init__(self, path: Path | None = None, audit: AuditLog | None = None) -> None:
        self._path = path
        self._audit = audit
        self._lock = threading.Lock()
        self._items: dict[str, Approval] = {}
        if path and path.exists():
            for raw in json.loads(path.read_text()):
                raw["capability"] = Capability(raw["capability"])
                raw["created_at"] = datetime.fromisoformat(raw["created_at"])
                raw["expires_at"] = datetime.fromisoformat(raw["expires_at"])
                self._items[raw["id"]] = Approval(**raw)

    def _save(self) -> None:
        if not self._path:
            return
        data = [
            {
                **a.__dict__,
                "capability": a.capability.value,
                "created_at": a.created_at.isoformat(),
                "expires_at": a.expires_at.isoformat(),
            }
            for a in self._items.values()
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def grant(
        self,
        capability: Capability,
        approved_by: str,
        scope: dict[str, Any] | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> Approval:
        """Called only from owner-facing entry points (CLI/dashboard), never by agents."""
        now = datetime.now(UTC)
        approval = Approval(
            id=secrets.token_hex(8),
            capability=capability,
            scope=scope or {},
            approved_by=approved_by,
            created_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            self._items[approval.id] = approval
            self._save()
        if self._audit:
            self._audit.append(approved_by, "approval.granted", {**approval.__dict__})
        return approval

    def consume(self, capability: Capability, scope: dict[str, Any] | None = None) -> Approval | None:
        """Atomically consume a matching, unexpired approval. Scope keys must all match."""
        now = datetime.now(UTC)
        wanted = scope or {}
        with self._lock:
            for approval in self._items.values():
                if (
                    approval.capability == capability
                    and not approval.consumed
                    and approval.expires_at > now
                    and all(wanted.get(k) == v for k, v in approval.scope.items())
                ):
                    approval.consumed = True
                    self._save()
                    if self._audit:
                        self._audit.append("system", "approval.consumed", {"id": approval.id})
                    return approval
        return None


class PermissionGate:
    def __init__(
        self,
        audit: AuditLog,
        approvals: ApprovalStore,
        allowlists: dict[str, frozenset[Capability]] | None = None,
    ) -> None:
        self._audit = audit
        self._approvals = approvals
        self._allowlists = dict(DEFAULT_AGENT_CAPABILITIES if allowlists is None else allowlists)

    def check(self, agent: str, capability: Capability, scope: dict[str, Any] | None = None) -> None:
        """Raise PermissionDenied unless allowed. Consumes an approval when one is required."""
        allowed = capability in self._allowlists.get(agent, frozenset())
        if not allowed:
            self._audit.append(agent, "permission.denied", {"capability": capability, "why": "not allowlisted"})
            raise PermissionDenied(f"{agent} is not allowlisted for {capability}")
        if capability in APPROVAL_REQUIRED and self._approvals.consume(capability, scope) is None:
            self._audit.append(agent, "permission.denied", {"capability": capability, "why": "no approval"})
            raise PermissionDenied(f"{capability} requires an owner approval")
        self._audit.append(agent, "permission.granted", {"capability": capability, "scope": scope or {}})
