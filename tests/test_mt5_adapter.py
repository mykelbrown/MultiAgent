"""MT5 adapter logic against a *fake* MetaTrader5 module.

These tests exercise Aegis's safety logic (demo-only, signature check, order_check before
order_send). They do NOT prove compatibility with the real terminal — see
docs/MT5_DEMO_CHECKLIST.md for the Windows manual test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aegis.core.audit import AuditLog
from aegis.core.config import TradingMode
from aegis.trading.brokers.base import BrokerError
from aegis.trading.brokers.mt5 import MT5DemoBroker
from aegis.trading.risk import ApprovedOrder, OrderSigner
from aegis.trading.signals import Side


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 1, 0, 1
    ORDER_TIME_GTC, ORDER_FILLING_FOK, ORDER_FILLING_IOC = 0, 0, 1
    POSITION_TYPE_BUY, DEAL_ENTRY_OUT = 0, 1

    def __init__(self, trade_mode: int = 0, check_retcode: int = 0) -> None:
        self.trade_mode = trade_mode
        self.check_retcode = check_retcode
        self.calls: list[str] = []

    def initialize(self, **kw):
        self.calls.append("initialize")
        return True

    def shutdown(self):
        self.calls.append("shutdown")

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(
            trade_mode=self.trade_mode, login=1, server="Demo", equity=1e4, balance=1e4, currency="USD"
        )

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=1.0850, ask=1.0851, time=1_700_000_000)

    def symbol_info(self, s):
        return SimpleNamespace(visible=True, filling_mode=2)

    def symbol_select(self, s, v):
        return True

    def order_check(self, req):
        self.calls.append("order_check")
        return SimpleNamespace(retcode=self.check_retcode, comment="Done")

    def order_send(self, req):
        self.calls.append("order_send")
        self.last_request = req
        return SimpleNamespace(retcode=10009, order=777, price=req["price"])

    def positions_get(self):
        return ()


def _order(signer: OrderSigner, mode=TradingMode.DEMO) -> ApprovedOrder:
    return signer.sign(
        ApprovedOrder(
            order_id="t-1",
            symbol="EURUSD",
            side=Side.BUY,
            lots=0.01,
            stop_loss=1.082,
            take_profit=1.09,
            max_deviation_points=150,
            mode=mode,
            source="test",
        )
    )


def test_refuses_real_account():
    fake = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    broker = MT5DemoBroker(OrderSigner(), AuditLog(), mt5_module=fake)
    with pytest.raises(BrokerError, match="not a DEMO"):
        broker.connect()
    assert fake.calls[-1] == "shutdown"


def test_order_check_runs_before_send_and_includes_sl():
    signer, fake = OrderSigner(), FakeMT5()
    broker = MT5DemoBroker(signer, AuditLog(), mt5_module=fake)
    broker.connect()
    res = broker.submit(_order(signer))
    assert res.ok and res.ticket == 777
    assert fake.calls[-2:] == ["order_check", "order_send"]
    assert fake.last_request["sl"] == 1.082 and fake.last_request["type_filling"] == FakeMT5.ORDER_FILLING_IOC


def test_failed_order_check_blocks_send():
    signer, fake = OrderSigner(), FakeMT5(check_retcode=10019)
    broker = MT5DemoBroker(signer, AuditLog(), mt5_module=fake)
    broker.connect()
    assert not broker.submit(_order(signer)).ok
    assert "order_send" not in fake.calls


def test_rejects_unsigned_and_wrong_mode():
    signer, fake = OrderSigner(), FakeMT5()
    broker = MT5DemoBroker(signer, AuditLog(), mt5_module=fake)
    broker.connect()
    assert not broker.submit(_order(OrderSigner())).ok
    assert not broker.submit(_order(signer, mode=TradingMode.LIVE)).ok
    assert "order_check" not in fake.calls
