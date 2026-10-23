"""Order/position reconciliation: local ledger vs broker truth. Any drift trips the kill switch."""

from __future__ import annotations

from pydantic import BaseModel

from aegis.core.audit import AuditLog
from aegis.trading.brokers.base import Broker
from aegis.trading.risk import KillSwitch, Position


class Ledger:
    """What Aegis believes is open. Updated only from confirmed broker fills/closes."""

    def __init__(self) -> None:
        self._open: dict[int, Position] = {}

    def record_open(self, position: Position) -> None:
        self._open[position.ticket] = position

    def record_closed(self, ticket: int) -> None:
        self._open.pop(ticket, None)

    def open_positions(self) -> dict[int, Position]:
        return dict(self._open)


class ReconciliationReport(BaseModel):
    ok: bool
    missing_at_broker: list[int] = []  # we think open, broker doesn't (closed by SL/TP or externally)
    unknown_at_broker: list[int] = []  # broker has, we didn't open (manual / other EA / bug)
    volume_mismatch: list[int] = []


class Reconciler:
    def __init__(self, ledger: Ledger, broker: Broker, kill_switch: KillSwitch, audit: AuditLog) -> None:
        self._ledger = ledger
        self._broker = broker
        self._kill = kill_switch
        self._audit = audit

    def run(self) -> ReconciliationReport:
        # Positions the broker confirms were closed (SL/TP hit, close order) are not drift.
        for t in self._broker.closed_tickets(set(self._ledger.open_positions())):
            self._ledger.record_closed(t)
            self._audit.append("reconciler", "ledger.closed_confirmed", {"ticket": t})
        local = self._ledger.open_positions()
        remote = {p.ticket: p for p in self._broker.positions()}
        report = ReconciliationReport(
            ok=True,
            missing_at_broker=sorted(set(local) - set(remote)),
            unknown_at_broker=sorted(set(remote) - set(local)),
            volume_mismatch=sorted(t for t in set(local) & set(remote) if abs(local[t].lots - remote[t].lots) > 1e-9),
        )
        report.ok = not (report.missing_at_broker or report.unknown_at_broker or report.volume_mismatch)
        self._audit.append("reconciler", "reconcile.run", report.model_dump())
        if not report.ok:
            self._kill.trip(f"reconciliation mismatch: {report.model_dump(exclude={'ok'})}", "reconciler")
        return report
