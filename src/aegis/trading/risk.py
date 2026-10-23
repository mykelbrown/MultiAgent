"""Hard risk limits, kill switch, and signed order approval.

Brokers accept only :class:`ApprovedOrder` objects carrying a valid HMAC from the
:class:`RiskEngine` that created them, so no other component (or agent) can mint an order
that skipped the checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from aegis.core.audit import AuditLog
from aegis.core.config import RiskLimits, TradingMode
from aegis.core.permissions import ApprovalStore, Capability, PermissionDenied
from aegis.trading.signals import Side, TradeSignal, pip_size

CONTRACT_SIZE = 100_000.0
MIN_LOT = 0.01
LOT_STEP = 0.01


class AccountState(BaseModel):
    equity: float = Field(gt=0)
    balance: float = Field(gt=0)
    currency: str
    mode: TradingMode


class Quote(BaseModel):
    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    ts: datetime


class Position(BaseModel):
    ticket: int
    symbol: str
    side: Side
    lots: float
    open_price: float
    stop_loss: float
    take_profit: float | None = None


class ApprovedOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    side: Side
    lots: float
    stop_loss: float
    take_profit: float | None
    max_deviation_points: int
    mode: TradingMode
    source: str
    signature: str = ""

    def signing_payload(self) -> bytes:
        data = self.model_dump(exclude={"signature"})
        return json.dumps(data, sort_keys=True, default=str).encode()


class RiskReason(StrEnum):
    KILL_SWITCH = "kill_switch_active"
    MODE_LOCKED = "trading_mode_locked"
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    UNSUPPORTED_CONVERSION = "unsupported_currency_conversion"
    PRICE_MOVED = "price_moved_beyond_deviation"
    MAX_POSITIONS = "max_open_positions"
    SYMBOL_EXPOSURE = "symbol_exposure_limit"
    TOTAL_EXPOSURE = "total_exposure_limit"
    DAILY_LOSS = "daily_loss_limit"
    BELOW_MIN_LOT = "size_below_min_lot"
    INVALID_STOP = "invalid_stop"


class RiskDecision(BaseModel):
    approved: bool
    order: ApprovedOrder | None = None
    reasons: list[RiskReason] = []
    detail: str = ""


class OrderSigner:
    """Per-process HMAC key; never persisted, never logged."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def sign(self, order: ApprovedOrder) -> ApprovedOrder:
        sig = hmac.new(self._key, order.signing_payload(), hashlib.sha256).hexdigest()
        return order.model_copy(update={"signature": sig})

    def verify(self, order: ApprovedOrder) -> bool:
        expected = hmac.new(self._key, order.signing_payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, order.signature)


class KillSwitch:
    """Persistent emergency stop. Anyone with TRIP may trip it; reset needs owner approval."""

    def __init__(self, path: Path | None, audit: AuditLog) -> None:
        self._path = path
        self._audit = audit
        self._lock = threading.Lock()
        self._state: dict[str, str | bool | None] = {"tripped": False, "reason": None, "at": None}
        if path and path.exists():
            self._state = json.loads(path.read_text())

    @property
    def tripped(self) -> bool:
        return bool(self._state["tripped"])

    @property
    def reason(self) -> str | None:
        r = self._state.get("reason")
        return str(r) if r is not None else None

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._state))

    def trip(self, reason: str, actor: str) -> None:
        with self._lock:
            already = self.tripped
            self._state = {"tripped": True, "reason": reason, "at": datetime.now(UTC).isoformat()}
            self._save()
        if not already:
            self._audit.append(actor, "killswitch.tripped", {"reason": reason})

    def reset(self, approvals: ApprovalStore, actor: str) -> None:
        if approvals.consume(Capability.RESET_KILL_SWITCH) is None:
            self._audit.append(actor, "killswitch.reset_denied", {})
            raise PermissionDenied("resetting the kill switch requires an owner approval")
        with self._lock:
            self._state = {"tripped": False, "reason": None, "at": None}
            self._save()
        self._audit.append(actor, "killswitch.reset", {})


class DailyLossTracker:
    """Tracks start-of-day equity (UTC day) to enforce the daily loss limit on equity."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._start_equity = 0.0

    def loss_pct(self, equity: float, now: datetime) -> float:
        today = now.astimezone(UTC).date()
        if self._day != today:
            self._day, self._start_equity = today, equity
        return max(0.0, (self._start_equity - equity) / self._start_equity * 100)


def quote_to_account_rate(symbol: str, price: float, account_currency: str) -> float:
    base, quote = symbol[:3], symbol[3:]
    if quote == account_currency:
        return 1.0
    if base == account_currency:
        return 1.0 / price
    raise ValueError(f"no direct conversion for {symbol} into {account_currency}")


def notional_in_account(symbol: str, lots: float, price: float, account_currency: str) -> float:
    return lots * CONTRACT_SIZE * price * quote_to_account_rate(symbol, price, account_currency)


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        symbol_allowlist: frozenset[str],
        kill_switch: KillSwitch,
        audit: AuditLog,
        signer: OrderSigner,
        allowed_modes: frozenset[TradingMode] = frozenset({TradingMode.PAPER, TradingMode.DEMO}),
    ) -> None:
        self._limits = limits
        self._symbols = symbol_allowlist
        self._kill = kill_switch
        self._audit = audit
        self._signer = signer
        self._modes = allowed_modes
        self._daily = DailyLossTracker()
        self._seq = 0

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def update_limits(self, new: RiskLimits, approvals: ApprovalStore, actor: str) -> None:
        increases = new.is_increase_over(self._limits)
        if increases and approvals.consume(Capability.INCREASE_RISK_LIMITS) is None:
            self._audit.append(actor, "risk.limits_change_denied", {"increases": increases})
            raise PermissionDenied(f"risk increase on {increases} requires owner approval")
        self._audit.append(actor, "risk.limits_changed", {"old": self._limits.model_dump(), "new": new.model_dump()})
        self._limits = new

    def check_daily_loss(self, account: AccountState, now: datetime) -> bool:
        """Returns True if within limit; trips the kill switch otherwise."""
        loss = self._daily.loss_pct(account.equity, now)
        if loss >= self._limits.daily_loss_limit_pct:
            self._kill.trip(f"daily loss {loss:.2f}% >= {self._limits.daily_loss_limit_pct}%", "risk_engine")
            return False
        return True

    def _reject(self, reason: RiskReason, detail: str, signal: TradeSignal) -> RiskDecision:
        self._audit.append(
            "risk_engine",
            "risk.rejected",
            {"reason": reason, "detail": detail, "symbol": signal.symbol, "msg": signal.message_id},
        )
        return RiskDecision(approved=False, reasons=[reason], detail=detail)

    def evaluate(
        self,
        signal: TradeSignal,
        account: AccountState,
        quote: Quote,
        positions: list[Position],
        now: datetime,
    ) -> RiskDecision:
        lim = self._limits
        if self._kill.tripped:
            return self._reject(RiskReason.KILL_SWITCH, self._kill.reason or "", signal)
        if account.mode not in self._modes:
            return self._reject(RiskReason.MODE_LOCKED, account.mode, signal)
        if signal.symbol not in self._symbols:
            return self._reject(RiskReason.SYMBOL_NOT_ALLOWED, signal.symbol, signal)
        if not self.check_daily_loss(account, now):
            return self._reject(RiskReason.DAILY_LOSS, self._kill.reason or "", signal)

        fill_price = quote.ask if signal.side is Side.BUY else quote.bid
        pip = pip_size(signal.symbol)
        if signal.entry is not None:
            deviation_pips = abs(fill_price - signal.entry) / pip
            if deviation_pips > lim.max_entry_deviation_pips:
                return self._reject(RiskReason.PRICE_MOVED, f"{deviation_pips:.1f} pips from entry", signal)

        # Stop must still be on the correct side of the actual fill price.
        if (signal.side is Side.BUY and signal.stop_loss >= fill_price) or (
            signal.side is Side.SELL and signal.stop_loss <= fill_price
        ):
            return self._reject(RiskReason.INVALID_STOP, "stop on wrong side of fill price", signal)
        stop_pips = abs(fill_price - signal.stop_loss) / pip
        if not lim.min_stop_distance_pips <= stop_pips <= lim.max_stop_distance_pips:
            return self._reject(RiskReason.INVALID_STOP, f"stop distance {stop_pips:.1f} pips", signal)

        if len(positions) >= lim.max_open_positions:
            return self._reject(RiskReason.MAX_POSITIONS, str(len(positions)), signal)

        try:
            rate = quote_to_account_rate(signal.symbol, fill_price, account.currency)
        except ValueError as exc:
            return self._reject(RiskReason.UNSUPPORTED_CONVERSION, str(exc), signal)

        # Position size from risk budget, then clamp by hard caps. Always round *down*.
        risk_amount = account.equity * lim.risk_per_trade_pct / 100
        loss_per_lot = abs(fill_price - signal.stop_loss) * CONTRACT_SIZE * rate
        raw_lots = risk_amount / loss_per_lot
        symbol_lots = sum(p.lots for p in positions if p.symbol == signal.symbol)
        cap = min(lim.max_lots_per_order, lim.max_lots_per_symbol - symbol_lots)
        lots = math.floor(min(raw_lots, cap) / LOT_STEP + 1e-9) * LOT_STEP
        lots = round(lots, 2)
        if symbol_lots + MIN_LOT > lim.max_lots_per_symbol + 1e-9:
            return self._reject(RiskReason.SYMBOL_EXPOSURE, f"{symbol_lots} lots open", signal)
        if lots < MIN_LOT:
            return self._reject(RiskReason.BELOW_MIN_LOT, f"computed {raw_lots:.4f}", signal)

        total_notional = sum(
            notional_in_account(p.symbol, p.lots, p.open_price, account.currency) for p in positions
        ) + notional_in_account(signal.symbol, lots, fill_price, account.currency)
        if total_notional / account.equity * 100 > lim.max_total_notional_pct:
            return self._reject(RiskReason.TOTAL_EXPOSURE, f"notional {total_notional:.0f}", signal)

        self._seq += 1
        order = self._signer.sign(
            ApprovedOrder(
                order_id=f"{now.strftime('%Y%m%d%H%M%S')}-{self._seq}",
                symbol=signal.symbol,
                side=signal.side,
                lots=lots,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profits[0] if signal.take_profits else None,
                max_deviation_points=int(lim.max_entry_deviation_pips * 10),
                mode=account.mode,
                source=f"telegram:{signal.source_chat_id}:{signal.message_id}",
            )
        )
        self._audit.append(
            "risk_engine",
            "risk.approved",
            {**order.model_dump(exclude={"signature"}), "risk_amount": round(risk_amount, 2)},
        )
        return RiskDecision(approved=True, order=order)
