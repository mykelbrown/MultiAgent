"""MetaTrader 5 adapter (DEMO accounts only in this release).

STATUS: written against the official MQL5 Python docs but **not yet executed** — the
MetaTrader5 package ships Windows wheels only and needs a running terminal. Run the manual
checklist in docs/MT5_DEMO_CHECKLIST.md on Windows before trusting it.

Safety properties:
* refuses to connect unless the logged-in account's trade_mode is DEMO;
* only accepts orders signed by the RiskEngine;
* always runs ``order_check`` before ``order_send``;
* credentials come from the terminal's existing login (or env vars), never from code.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from aegis.core.audit import AuditLog
from aegis.core.config import TradingMode
from aegis.trading.brokers.base import BrokerError, OrderResult
from aegis.trading.risk import AccountState, ApprovedOrder, OrderSigner, Position, Quote
from aegis.trading.signals import Side

TRADE_RETCODE_DONE = 10009  # per MQL5 docs (order_send example)
ORDER_CHECK_OK = 0
MAGIC = 0xAE615  # identifies Aegis orders in the terminal


def _load_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: N813
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise BrokerError("MetaTrader5 package unavailable (Windows only: uv sync --extra mt5)") from exc
    return mt5


class MT5DemoBroker:
    mode = TradingMode.DEMO

    def __init__(self, signer: OrderSigner, audit: AuditLog, mt5_module: Any | None = None) -> None:
        self._signer = signer
        self._audit = audit
        self._mt5 = mt5_module or _load_mt5()
        self._connected = False

    def connect(self) -> None:
        mt5 = self._mt5
        kwargs: dict[str, Any] = {}
        if path := os.environ.get("AEGIS_MT5_TERMINAL_PATH"):
            kwargs["path"] = path
        if not mt5.initialize(**kwargs):
            raise BrokerError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            raise BrokerError("MT5 has no logged-in account")
        if info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            self._audit.append("mt5", "broker.refused_non_demo", {"login": info.login, "trade_mode": info.trade_mode})
            raise BrokerError("refusing to trade: account is not a DEMO account")
        self._connected = True
        self._audit.append("mt5", "broker.connected", {"login": info.login, "server": info.server})

    def _require(self) -> None:
        if not self._connected:
            raise BrokerError("not connected")

    def account(self) -> AccountState:
        self._require()
        info = self._mt5.account_info()
        return AccountState(equity=info.equity, balance=info.balance, currency=info.currency, mode=self.mode)

    def quote(self, symbol: str) -> Quote:
        self._require()
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"no tick for {symbol}")
        return Quote(symbol=symbol, bid=tick.bid, ask=tick.ask, ts=datetime.fromtimestamp(tick.time, UTC))

    def positions(self) -> list[Position]:
        self._require()
        raw = self._mt5.positions_get() or ()
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side=Side.BUY if p.type == self._mt5.POSITION_TYPE_BUY else Side.SELL,
                lots=p.volume,
                open_price=p.price_open,
                stop_loss=p.sl,
                take_profit=p.tp or None,
            )
            for p in raw
        ]

    def _filling(self, symbol: str) -> int:
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"unknown symbol {symbol}")
        if not info.visible and not self._mt5.symbol_select(symbol, True):
            raise BrokerError(f"cannot select {symbol}")
        # filling_mode is a bitmask: 1=FOK, 2=IOC (SYMBOL_FILLING_*).
        return int(self._mt5.ORDER_FILLING_IOC if info.filling_mode & 2 else self._mt5.ORDER_FILLING_FOK)

    def submit(self, order: ApprovedOrder) -> OrderResult:
        self._require()
        mt5 = self._mt5
        if not self._signer.verify(order) or order.mode is not self.mode:
            self._audit.append("mt5", "order.rejected", {"order_id": order.order_id, "why": "signature/mode"})
            return OrderResult(ok=False, order_id=order.order_id, message="invalid risk signature or mode")
        q = self.quote(order.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.lots,
            "type": mt5.ORDER_TYPE_BUY if order.side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": q.ask if order.side is Side.BUY else q.bid,
            "sl": order.stop_loss,
            "tp": order.take_profit or 0.0,
            "deviation": order.max_deviation_points,
            "magic": MAGIC,
            "comment": order.order_id[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(order.symbol),
        }
        check = mt5.order_check(request)
        if check is None or check.retcode != ORDER_CHECK_OK:
            self._audit.append("mt5", "order.check_failed", {"order_id": order.order_id, "check": str(check)})
            return OrderResult(ok=False, order_id=order.order_id, message=f"order_check failed: {check}")
        result = mt5.order_send(request)
        if result is None or result.retcode != TRADE_RETCODE_DONE:
            self._audit.append("mt5", "order.send_failed", {"order_id": order.order_id, "result": str(result)})
            return OrderResult(ok=False, order_id=order.order_id, message=f"order_send failed: {result}")
        self._audit.append(
            "mt5", "order.filled", {"order_id": order.order_id, "ticket": result.order, "price": result.price}
        )
        return OrderResult(ok=True, order_id=order.order_id, ticket=result.order, fill_price=result.price)

    def close_all(self, reason: str) -> list[OrderResult]:
        self._require()
        mt5 = self._mt5
        results = []
        for p in self.positions():
            q = self.quote(p.symbol)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.lots,
                "type": mt5.ORDER_TYPE_SELL if p.side is Side.BUY else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": q.bid if p.side is Side.BUY else q.ask,
                "deviation": 50,
                "magic": MAGIC,
                "comment": f"aegis close {reason}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(p.symbol),
            }
            r = mt5.order_send(request)
            ok = r is not None and r.retcode == TRADE_RETCODE_DONE
            self._audit.append("mt5", "position.close", {"ticket": p.ticket, "ok": ok, "reason": reason})
            results.append(OrderResult(ok=ok, order_id=f"close-{p.ticket}", ticket=p.ticket, message=str(r)))
        return results

    def closed_tickets(self, tickets: set[int]) -> set[int]:
        self._require()
        closed = set()
        for ticket in tickets:
            deals = self._mt5.history_deals_get(position=ticket) or ()
            if any(d.entry == self._mt5.DEAL_ENTRY_OUT for d in deals):
                closed.add(ticket)
        return closed

    def shutdown(self) -> None:
        self._mt5.shutdown()
        self._connected = False


class MT5ReadOnly:
    """Read-only MT5 access (account info + ticks). Never sends orders; works on any account type,
    used for the setup check and as a live price feed for paper trading."""

    def __init__(self, mt5_module: Any | None = None) -> None:
        self._mt5 = mt5_module or _load_mt5()
        kwargs: dict[str, Any] = {}
        if path := os.environ.get("AEGIS_MT5_TERMINAL_PATH"):
            kwargs["path"] = path
        if not self._mt5.initialize(**kwargs):
            raise BrokerError(f"MT5 initialize failed: {self._mt5.last_error()}")

    def account_summary(self) -> dict[str, Any]:
        info = self._mt5.account_info()
        if info is None:
            raise BrokerError("MT5 has no logged-in account")
        return {
            "is_demo": info.trade_mode == self._mt5.ACCOUNT_TRADE_MODE_DEMO,
            "server": info.server,
            "currency": info.currency,
            "balance": info.balance,
            "leverage": info.leverage,
            "trade_allowed": bool(getattr(info, "trade_allowed", False)),
        }

    def quote(self, symbol: str) -> Quote:
        if not self._mt5.symbol_select(symbol, True):
            raise BrokerError(f"cannot select {symbol}")
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"no tick for {symbol}")
        return Quote(symbol=symbol, bid=tick.bid, ask=tick.ask, ts=datetime.fromtimestamp(tick.time, UTC))

    def shutdown(self) -> None:
        self._mt5.shutdown()
