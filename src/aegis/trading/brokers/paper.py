"""In-process paper broker: deterministic fills at bid/ask, SL/TP simulation, P&L accounting."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from aegis.core.audit import AuditLog
from aegis.core.config import TradingMode
from aegis.trading.brokers.base import BrokerError, OrderResult
from aegis.trading.risk import (
    CONTRACT_SIZE,
    AccountState,
    ApprovedOrder,
    OrderSigner,
    Position,
    Quote,
    quote_to_account_rate,
)
from aegis.trading.signals import Side


class PaperBroker:
    mode = TradingMode.PAPER

    def __init__(self, signer: OrderSigner, audit: AuditLog, balance: float = 10_000.0, currency: str = "USD") -> None:
        self._signer = signer
        self._audit = audit
        self._balance = balance
        self._currency = currency
        self._quotes: dict[str, Quote] = {}
        self._positions: dict[int, Position] = {}
        self._tickets = itertools.count(1)
        self._closed: set[int] = set()

    # --- market data -------------------------------------------------------------------------
    def set_quote(self, symbol: str, bid: float, ask: float, ts: datetime | None = None) -> None:
        self._quotes[symbol] = Quote(symbol=symbol, bid=bid, ask=ask, ts=ts or datetime.now(UTC))
        self._check_stops(symbol)

    def quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes:
            raise BrokerError(f"no quote for {symbol}")
        return self._quotes[symbol]

    # --- account -----------------------------------------------------------------------------
    def _pnl(self, p: Position, close_price: float) -> float:
        direction = 1 if p.side is Side.BUY else -1
        raw = (close_price - p.open_price) * direction * p.lots * CONTRACT_SIZE
        return raw * quote_to_account_rate(p.symbol, close_price, self._currency)

    def _exit_price(self, p: Position) -> float:
        q = self._quotes[p.symbol]
        return q.bid if p.side is Side.BUY else q.ask

    def account(self) -> AccountState:
        floating = sum(self._pnl(p, self._exit_price(p)) for p in self._positions.values())
        return AccountState(
            equity=self._balance + floating, balance=self._balance, currency=self._currency, mode=self.mode
        )

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    # --- orders ------------------------------------------------------------------------------
    def submit(self, order: ApprovedOrder) -> OrderResult:
        if not self._signer.verify(order):
            self._audit.append("paper_broker", "order.rejected", {"order_id": order.order_id, "why": "bad signature"})
            return OrderResult(ok=False, order_id=order.order_id, message="invalid risk signature")
        if order.mode is not self.mode:
            return OrderResult(
                ok=False, order_id=order.order_id, message=f"order mode {order.mode} != broker {self.mode}"
            )
        q = self.quote(order.symbol)
        price = q.ask if order.side is Side.BUY else q.bid
        ticket = next(self._tickets)
        self._positions[ticket] = Position(
            ticket=ticket,
            symbol=order.symbol,
            side=order.side,
            lots=order.lots,
            open_price=price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
        )
        self._audit.append(
            "paper_broker", "order.filled", {"order_id": order.order_id, "ticket": ticket, "price": price}
        )
        return OrderResult(ok=True, order_id=order.order_id, ticket=ticket, fill_price=price)

    def _close(self, ticket: int, reason: str) -> OrderResult:
        p = self._positions.pop(ticket)
        self._closed.add(ticket)
        price = self._exit_price(p)
        pnl = self._pnl(p, price)
        self._balance += pnl
        self._audit.append(
            "paper_broker",
            "position.closed",
            {"ticket": ticket, "price": price, "pnl": round(pnl, 2), "reason": reason},
        )
        return OrderResult(ok=True, order_id=f"close-{ticket}", ticket=ticket, fill_price=price, message=reason)

    def _check_stops(self, symbol: str) -> None:
        q = self._quotes[symbol]
        for p in [p for p in self._positions.values() if p.symbol == symbol]:
            if p.side is Side.BUY:
                hit_sl, hit_tp = q.bid <= p.stop_loss, p.take_profit is not None and q.bid >= p.take_profit
            else:
                hit_sl, hit_tp = q.ask >= p.stop_loss, p.take_profit is not None and q.ask <= p.take_profit
            if hit_sl or hit_tp:
                self._close(p.ticket, "stop_loss" if hit_sl else "take_profit")

    def close_all(self, reason: str) -> list[OrderResult]:
        return [self._close(t, reason) for t in list(self._positions)]

    def closed_tickets(self, tickets: set[int]) -> set[int]:
        return tickets & self._closed

    # Test hook: simulate out-of-band broker-side change (for reconciliation tests).
    def _inject_external_position(self, position: Position) -> None:
        self._positions[position.ticket] = position
