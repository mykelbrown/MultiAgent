from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from aegis.core.config import TradingMode
from aegis.trading.risk import AccountState, ApprovedOrder, Position, Quote


class OrderResult(BaseModel):
    ok: bool
    order_id: str
    ticket: int | None = None
    fill_price: float | None = None
    message: str = ""


class Broker(Protocol):
    mode: TradingMode

    def account(self) -> AccountState: ...
    def quote(self, symbol: str) -> Quote: ...
    def positions(self) -> list[Position]: ...
    def submit(self, order: ApprovedOrder) -> OrderResult: ...
    def close_all(self, reason: str) -> list[OrderResult]: ...
    def closed_tickets(self, tickets: set[int]) -> set[int]:
        """Subset of ``tickets`` the broker confirms were closed (SL/TP/close order)."""
        ...


class BrokerError(Exception):
    pass
