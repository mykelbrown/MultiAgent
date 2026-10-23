from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aegis.agents.telegram_intake import TelegramIntakeAgent
from aegis.core.audit import AuditLog
from aegis.core.config import RiskLimits, Settings
from aegis.core.permissions import ApprovalStore, PermissionGate
from aegis.trading.brokers.paper import PaperBroker
from aegis.trading.reconcile import Ledger, Reconciler
from aegis.trading.risk import KillSwitch, OrderSigner, RiskEngine
from aegis.trading.signals import RawMessage, SignalValidator

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
SOURCE = -1001234567890


def msg(text: str, message_id: int = 1, posted_at: datetime = NOW, source: int = SOURCE) -> RawMessage:
    return RawMessage(source_chat_id=source, message_id=message_id, text=text, posted_at=posted_at)


class Stack:
    def __init__(self, tmp_path, limits: RiskLimits | None = None) -> None:
        self.settings = Settings(telegram_source_allowlist=frozenset({SOURCE}), risk=limits or RiskLimits())
        self.audit = AuditLog(tmp_path / "audit.db")
        self.approvals = ApprovalStore(tmp_path / "approvals.json", self.audit)
        self.gate = PermissionGate(self.audit, self.approvals)
        self.kill = KillSwitch(tmp_path / "kill.json", self.audit)
        self.signer = OrderSigner()
        self.risk = RiskEngine(self.settings.risk, self.settings.symbol_allowlist, self.kill, self.audit, self.signer)
        self.broker = PaperBroker(self.signer, self.audit, balance=10_000)
        self.broker.set_quote("EURUSD", 1.08500, 1.08510, NOW)
        self.broker.set_quote("USDJPY", 150.000, 150.010, NOW)
        self.broker.set_quote("GBPUSD", 1.30000, 1.30015, NOW)
        self.validator = SignalValidator(
            self.settings.telegram_source_allowlist, self.settings.symbol_allowlist, self.settings.risk
        )
        self.ledger = Ledger()
        self.reconciler = Reconciler(self.ledger, self.broker, self.kill, self.audit)
        self.agent = TelegramIntakeAgent(
            self.gate, self.validator, self.risk, self.broker, self.ledger, self.kill, self.audit
        )


@pytest.fixture
def stack(tmp_path) -> Stack:
    return Stack(tmp_path)
