from __future__ import annotations

from datetime import timedelta

import pytest

from aegis.core.config import RiskLimits, TradingMode
from aegis.core.permissions import Capability, PermissionDenied
from aegis.trading.risk import AccountState, OrderSigner, Position, RiskReason
from aegis.trading.signals import Side, parse_signal
from tests.conftest import NOW, Stack, msg

SYMBOLS = frozenset({"EURUSD", "GBPUSD", "USDJPY"})


def _eval(stack: Stack, text: str, positions=None, account=None):
    sig = parse_signal(msg(text), SYMBOLS)
    return stack.risk.evaluate(
        sig, account or stack.broker.account(), stack.broker.quote(sig.symbol), positions or [], NOW
    )


def test_position_size_from_risk_budget(stack):
    # equity 10k, 0.5% = $50 risk; stop = 1.08510-1.08200 = 31 pips -> $310/lot -> 0.16 lots, capped at 0.10
    d = _eval(stack, "BUY EURUSD 1.0851 SL 1.0820 TP 1.0900")
    assert d.approved and d.order.lots == pytest.approx(0.10)
    # Wider stop: 1.0851-1.0751 = 100 pips -> $1000/lot -> 0.05 lots (rounded down)
    d = _eval(stack, "BUY EURUSD 1.0851 SL 1.0751")
    assert d.order.lots == pytest.approx(0.05)


def test_usd_base_pair_conversion(stack):
    # USDJPY: 40 pip stop at 150.00 => loss/lot = 0.40*100000/150 = $266.67 -> 50/266.67 = 0.18 -> cap 0.10
    d = _eval(stack, "SELL USDJPY 150.00 SL 150.40")
    assert d.approved and d.order.lots == pytest.approx(0.10)


def test_price_moved_rejection(stack):
    d = _eval(stack, "BUY EURUSD 1.0800 SL 1.0770")  # market is 1.0851 -> 51 pips away
    assert d.reasons == [RiskReason.PRICE_MOVED]


def test_max_positions(stack):
    pos = [
        Position(ticket=i, symbol="GBPUSD", side=Side.BUY, lots=0.01, open_price=1.3, stop_loss=1.29) for i in range(3)
    ]
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", positions=pos).reasons == [RiskReason.MAX_POSITIONS]


def test_symbol_exposure(stack):
    pos = [Position(ticket=1, symbol="EURUSD", side=Side.BUY, lots=0.20, open_price=1.085, stop_loss=1.08)]
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", positions=pos).reasons == [RiskReason.SYMBOL_EXPOSURE]
    pos = [Position(ticket=1, symbol="EURUSD", side=Side.BUY, lots=0.15, open_price=1.085, stop_loss=1.08)]
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", positions=pos).order.lots == pytest.approx(0.05)


def test_total_exposure(tmp_path):
    s = Stack(tmp_path, RiskLimits(max_total_notional_pct=100))
    pos = [Position(ticket=1, symbol="GBPUSD", side=Side.BUY, lots=0.05, open_price=1.3, stop_loss=1.29)]
    assert _eval(s, "BUY EURUSD 1.0851 SL 1.0820", positions=pos).reasons == [RiskReason.TOTAL_EXPOSURE]


def test_below_min_lot(stack):
    tiny = AccountState(equity=100, balance=100, currency="USD", mode=TradingMode.PAPER)
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", account=tiny).reasons == [RiskReason.BELOW_MIN_LOT]


def test_live_mode_locked(stack):
    live = AccountState(equity=10_000, balance=10_000, currency="USD", mode=TradingMode.LIVE)
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", account=live).reasons == [RiskReason.MODE_LOCKED]


def test_unsupported_cross_conversion(tmp_path):
    from aegis.core.config import Settings

    s = Stack(tmp_path)
    s.risk._symbols = frozenset({"EURGBP"})
    s.broker.set_quote("EURGBP", 0.8300, 0.8301, NOW)
    sig = parse_signal(msg("BUY EURGBP 0.8301 SL 0.8270"), frozenset({"EURGBP"}))
    d = s.risk.evaluate(sig, s.broker.account(), s.broker.quote("EURGBP"), [], NOW)
    assert d.reasons == [RiskReason.UNSUPPORTED_CONVERSION]
    assert Settings  # imported for type completeness


def test_daily_loss_trips_kill_switch(stack):
    start = AccountState(equity=10_000, balance=10_000, currency="USD", mode=TradingMode.PAPER)
    assert stack.risk.check_daily_loss(start, NOW)
    down = AccountState(equity=9_790, balance=10_000, currency="USD", mode=TradingMode.PAPER)
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820", account=down).reasons == [RiskReason.DAILY_LOSS]
    assert stack.kill.tripped
    # New UTC day resets the baseline, but the kill switch stays tripped until owner reset.
    assert _eval(stack, "BUY EURUSD 1.0851 SL 1.0820").reasons == [RiskReason.KILL_SWITCH]


def test_kill_switch_reset_requires_approval_and_persists(tmp_path, stack):
    stack.kill.trip("manual", "owner")
    from aegis.trading.risk import KillSwitch

    assert KillSwitch(tmp_path / "kill.json", stack.audit).tripped  # survives restart
    with pytest.raises(PermissionDenied):
        stack.kill.reset(stack.approvals, "owner")
    stack.approvals.grant(Capability.RESET_KILL_SWITCH, "owner")
    stack.kill.reset(stack.approvals, "owner")
    assert not stack.kill.tripped


def test_risk_increase_needs_approval_decrease_does_not(stack):
    stack.risk.update_limits(RiskLimits(risk_per_trade_pct=0.25), stack.approvals, "owner")
    with pytest.raises(PermissionDenied):
        stack.risk.update_limits(RiskLimits(risk_per_trade_pct=1.0), stack.approvals, "agent")
    assert stack.risk.limits.risk_per_trade_pct == 0.25
    stack.approvals.grant(Capability.INCREASE_RISK_LIMITS, "owner")
    stack.risk.update_limits(RiskLimits(risk_per_trade_pct=1.0), stack.approvals, "owner")
    assert stack.risk.limits.risk_per_trade_pct == 1.0


def test_broker_rejects_unsigned_or_forged_orders(stack):
    order = _eval(stack, "BUY EURUSD 1.0851 SL 1.0820").order
    forged = order.model_copy(update={"lots": 5.0})  # tampered after signing
    assert not stack.broker.submit(forged).ok
    other_signer = OrderSigner()
    assert not stack.broker.submit(other_signer.sign(order.model_copy(update={"signature": ""}))).ok
    assert stack.broker.submit(order).ok


# --- end-to-end paper pipeline -------------------------------------------------------------------


def test_end_to_end_paper_trade_and_take_profit(stack):
    out = stack.agent.handle(msg("BUY EURUSD 1.0851 SL 1.0820 TP 1.0900"), NOW)
    assert out.stage == "executed" and out.accepted and out.ticket == 1
    assert stack.reconciler.run().ok
    stack.broker.set_quote("EURUSD", 1.0901, 1.0902)  # TP hit
    assert stack.broker.positions() == []
    assert stack.broker.account().balance == pytest.approx(10_000 + (1.0901 - 1.0851) * 0.10 * 100_000)
    assert stack.reconciler.run().ok  # closed by TP is not drift
    ok, _ = stack.audit.verify()
    assert ok
    actions = [r.action for r in stack.audit.records()]
    for expected in ("signal.received", "risk.approved", "order.filled", "position.closed", "reconcile.run"):
        assert expected in actions


def test_pipeline_rejections_are_audited(stack):
    assert stack.agent.handle(msg("BUY EURUSD 1.0851", message_id=5), NOW).stage == "validation"
    assert stack.agent.handle(msg("BUY EURUSD 1.0700 SL 1.0670", message_id=6), NOW).stage == "risk"
    outcomes = stack.audit.records("signal.outcome")
    assert [o.payload["stage"] for o in outcomes] == ["validation", "risk"]


def test_reconciliation_mismatch_trips_kill_switch(stack):
    stack.agent.handle(msg("BUY EURUSD 1.0851 SL 1.0820"), NOW)
    stack.broker._inject_external_position(
        Position(ticket=99, symbol="GBPUSD", side=Side.SELL, lots=1.0, open_price=1.3, stop_loss=1.31)
    )
    report = stack.reconciler.run()
    assert not report.ok and report.unknown_at_broker == [99]
    assert stack.kill.tripped
    out = stack.agent.handle(msg("SELL GBPUSD 1.3000 SL 1.3030", message_id=2), NOW + timedelta(seconds=1))
    assert out.stage == "risk" and "kill_switch_active" in out.reasons


def test_emergency_stop_flattens(stack):
    stack.agent.handle(msg("BUY EURUSD 1.0851 SL 1.0820"), NOW)
    stack.agent.handle(msg("SELL GBPUSD 1.3000 SL 1.3030", message_id=2), NOW)
    assert len(stack.broker.positions()) == 2
    stack.agent.emergency_stop("owner pressed kill", "owner")
    assert stack.broker.positions() == [] and stack.kill.tripped
    assert stack.reconciler.run().ok


def test_agent_without_permission_cannot_trade(stack):
    from aegis.core.permissions import PermissionGate

    stack.agent._gate = PermissionGate(stack.audit, stack.approvals, allowlists={})
    out = stack.agent.handle(msg("BUY EURUSD 1.0851 SL 1.0820"), NOW)
    assert out.stage == "permission" and stack.broker.positions() == []
