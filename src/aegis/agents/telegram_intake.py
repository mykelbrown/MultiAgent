"""Agent 2: Telegram Signal Intake & MT5 Execution (paper/demo only).

Pipeline per message:
  permission gate -> SignalValidator -> RiskEngine -> Broker.submit -> Ledger -> audit
Each stage writes to the audit log; failures stop the pipeline for that message.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from aegis.core.audit import AuditLog
from aegis.core.config import TradingMode
from aegis.core.permissions import Capability, PermissionDenied, PermissionGate
from aegis.trading.brokers.base import Broker, BrokerError
from aegis.trading.reconcile import Ledger
from aegis.trading.risk import KillSwitch, Position, RiskEngine
from aegis.trading.signals import RawMessage, Side, SignalValidator

AGENT = "telegram_intake"

_MODE_CAPABILITY = {
    TradingMode.PAPER: Capability.PLACE_PAPER_ORDER,
    TradingMode.DEMO: Capability.PLACE_DEMO_ORDER,
    TradingMode.LIVE: Capability.PLACE_LIVE_ORDER,
}


class IntakeOutcome(BaseModel):
    message_id: int
    stage: str  # "permission" | "validation" | "risk" | "broker" | "executed"
    accepted: bool
    reasons: list[str] = []
    ticket: int | None = None


class TelegramIntakeAgent:
    def __init__(
        self,
        gate: PermissionGate,
        validator: SignalValidator,
        risk: RiskEngine,
        broker: Broker,
        ledger: Ledger,
        kill_switch: KillSwitch,
        audit: AuditLog,
    ) -> None:
        self._gate = gate
        self._validator = validator
        self._risk = risk
        self._broker = broker
        self._ledger = ledger
        self._kill = kill_switch
        self._audit = audit

    def handle(self, msg: RawMessage, now: datetime) -> IntakeOutcome:
        # Raw text is redacted by the audit layer; it is stored as data only.
        self._audit.append(AGENT, "signal.received", msg.model_dump())

        def out(stage: str, accepted: bool, reasons: list[str], ticket: int | None = None) -> IntakeOutcome:
            o = IntakeOutcome(message_id=msg.message_id, stage=stage, accepted=accepted, reasons=reasons, ticket=ticket)
            self._audit.append(AGENT, "signal.outcome", o.model_dump())
            return o

        try:
            self._gate.check(AGENT, Capability.READ_TELEGRAM_SIGNALS)
            self._gate.check(AGENT, _MODE_CAPABILITY[self._broker.mode])
        except PermissionDenied as exc:
            return out("permission", False, [str(exc)])

        decision = self._validator.evaluate(msg, now, price_lookup=self._live_price)
        if not decision.accepted or decision.signal is None:
            return out("validation", False, [r.value for r in decision.reasons] + [decision.detail])
        signal = decision.signal

        try:
            quote = self._broker.quote(signal.symbol)
            account = self._broker.account()
            positions = self._broker.positions()
        except BrokerError as exc:
            return out("broker", False, [f"market data unavailable: {exc}"])

        risk = self._risk.evaluate(signal, account, quote, positions, now)
        if not risk.approved or risk.order is None:
            return out("risk", False, [r.value for r in risk.reasons] + [risk.detail])

        result = self._broker.submit(risk.order)
        if not result.ok or result.ticket is None:
            return out("broker", False, [result.message])
        self._ledger.record_open(
            Position(
                ticket=result.ticket,
                symbol=risk.order.symbol,
                side=risk.order.side,
                lots=risk.order.lots,
                open_price=result.fill_price or 0.0,
                stop_loss=risk.order.stop_loss,
                take_profit=risk.order.take_profit,
            )
        )
        return out("executed", True, [], ticket=result.ticket)

    def _live_price(self, symbol: str, side: Side) -> float | None:
        try:
            q = self._broker.quote(symbol)
        except BrokerError:
            return None
        return q.ask if side is Side.BUY else q.bid

    def emergency_stop(self, reason: str, actor: str) -> None:
        """Kill switch: block new orders and flatten all positions."""
        self._kill.trip(reason, actor)
        results = self._broker.close_all(f"kill switch: {reason}")
        for r in results:
            if r.ok and r.ticket is not None:
                self._ledger.record_closed(r.ticket)
        self._audit.append(actor, "killswitch.flatten", {"closed": [r.ticket for r in results if r.ok]})
