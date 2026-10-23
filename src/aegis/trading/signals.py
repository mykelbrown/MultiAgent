"""Telegram trade-signal schema, deterministic parser, and stateful validator.

No LLM is involved: signal text is untrusted input and is only ever matched against strict
patterns. Anything ambiguous is rejected rather than guessed.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis.core.config import RiskLimits


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class RejectReason(StrEnum):
    UNTRUSTED_SOURCE = "untrusted_source"
    MALFORMED = "malformed"
    MISSING_SYMBOL = "missing_symbol"
    UNKNOWN_SYMBOL = "unknown_symbol"
    MULTIPLE_SYMBOLS = "multiple_symbols"
    MISSING_SIDE = "missing_side"
    CONTRADICTORY_SIDE = "contradictory_side"
    MISSING_STOP_LOSS = "missing_stop_loss"
    AMBIGUOUS_STOP_LOSS = "ambiguous_stop_loss"
    AMBIGUOUS_ENTRY = "ambiguous_entry"
    MISSING_ENTRY = "missing_entry"
    TOO_MANY_TAKE_PROFITS = "too_many_take_profits"
    UNSUPPORTED_INSTRUCTION = "unsupported_instruction"
    INVALID_GEOMETRY = "invalid_geometry"
    STOP_TOO_TIGHT = "stop_too_tight"
    STOP_TOO_WIDE = "stop_too_wide"
    STALE = "stale"
    FUTURE_TIMESTAMP = "future_timestamp"
    DUPLICATE = "duplicate"


class RawMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_chat_id: int
    message_id: int
    text: str = Field(max_length=4096)
    posted_at: datetime

    @field_validator("posted_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("posted_at must be timezone-aware")
        return v.astimezone(UTC)


class TradeSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    order_type: OrderType
    entry: float | None = Field(None, gt=0)
    stop_loss: float = Field(gt=0)
    take_profits: tuple[float, ...] = ()
    source_chat_id: int
    message_id: int
    posted_at: datetime

    @property
    def fingerprint(self) -> str:
        """Content identity used for duplicate detection across re-posts/forwards."""
        material = f"{self.symbol}|{self.side}|{self.order_type}|{self.entry}|{self.stop_loss}|{self.take_profits}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]


class SignalDecision(BaseModel):
    accepted: bool
    signal: TradeSignal | None = None
    reasons: list[RejectReason] = []
    detail: str = ""


class SignalRejected(Exception):
    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


# --- Parsing ---------------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"
_SL_RE = re.compile(rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b\s*[:=@]?\s*{_NUM}")
# Optional TP index ("TP1", "TP 2") must be a lone digit, so "TP 150.80" isn't read as TP#1 @ 50.80.
_TP_RE = re.compile(rf"\b(?:TP|T/P|TAKE\s*PROFIT)(?:\s*\d(?![\d.]))?\s*[:=@]?\s*{_NUM}")
_TYPE_RE = re.compile(r"\b(BUY|SELL)\s+(LIMIT|STOP)\b(?!\s*LOSS)")
_BUY_RE = re.compile(r"\b(?:BUY|LONG)\b")
_SELL_RE = re.compile(r"\b(?:SELL|SHORT)\b")
_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*/?\s*([A-Z]{3})\b")
_MGMT_RE = re.compile(r"\b(CLOSE|CANCEL|CANCELLED|MODIFY|MOVE\s+SL|BREAK\s*EVEN|BREAKEVEN|PARTIAL|HIT|UPDATE)\b")
_RANGE_RE = re.compile(rf"{_NUM}\s*[-–~]\s*{_NUM}")
_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "XAU", "XAG"}
MAX_TAKE_PROFITS = 5


def parse_signal(msg: RawMessage, symbol_allowlist: frozenset[str]) -> TradeSignal:
    """Parse one message into a TradeSignal, or raise SignalRejected. Stateless."""
    text = msg.text.upper().replace("–", "-")
    if not text.strip():
        raise SignalRejected(RejectReason.MALFORMED, "empty message")
    if _MGMT_RE.search(text):
        raise SignalRejected(RejectReason.UNSUPPORTED_INSTRUCTION, "trade-management message")

    pairs = {a + b for a, b in _PAIR_RE.findall(text) if a in _CURRENCIES and b in _CURRENCIES}
    if not pairs:
        raise SignalRejected(RejectReason.MISSING_SYMBOL)
    if len(pairs) > 1:
        raise SignalRejected(RejectReason.MULTIPLE_SYMBOLS, ",".join(sorted(pairs)))
    symbol = pairs.pop()
    if symbol not in symbol_allowlist:
        raise SignalRejected(RejectReason.UNKNOWN_SYMBOL, symbol)

    has_buy, has_sell = bool(_BUY_RE.search(text)), bool(_SELL_RE.search(text))
    if has_buy and has_sell:
        raise SignalRejected(RejectReason.CONTRADICTORY_SIDE)
    if not (has_buy or has_sell):
        raise SignalRejected(RejectReason.MISSING_SIDE)
    side = Side.BUY if has_buy else Side.SELL

    type_match = _TYPE_RE.search(text)
    order_type = OrderType(type_match.group(2)) if type_match else OrderType.MARKET

    sls = {float(v) for v in _SL_RE.findall(text)}
    if not sls:
        raise SignalRejected(RejectReason.MISSING_STOP_LOSS)
    if len(sls) > 1:
        raise SignalRejected(RejectReason.AMBIGUOUS_STOP_LOSS)
    stop_loss = sls.pop()

    tps = tuple(dict.fromkeys(float(v) for v in _TP_RE.findall(text)))
    if len(tps) > MAX_TAKE_PROFITS:
        raise SignalRejected(RejectReason.TOO_MANY_TAKE_PROFITS)

    # Entry = the numbers left after removing SL/TP clauses and the symbol.
    remainder = _TP_RE.sub(" ", _SL_RE.sub(" ", text))
    if _RANGE_RE.search(remainder):
        raise SignalRejected(RejectReason.AMBIGUOUS_ENTRY, "entry zone/range")
    entries = {float(v) for v in re.findall(rf"(?<![\w.]){_NUM}(?![\w]|\.\d)", remainder)}
    if len(entries) > 1:
        raise SignalRejected(RejectReason.AMBIGUOUS_ENTRY, f"{len(entries)} candidate prices")
    entry = entries.pop() if entries else None
    if entry is None and order_type is not OrderType.MARKET:
        raise SignalRejected(RejectReason.MISSING_ENTRY, "pending order needs a price")

    return TradeSignal(
        symbol=symbol,
        side=side,
        order_type=order_type,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=tps,
        source_chat_id=msg.source_chat_id,
        message_id=msg.message_id,
        posted_at=msg.posted_at,
    )


# --- Validation ------------------------------------------------------------------------------


def check_geometry(signal: TradeSignal, reference_price: float, limits: RiskLimits) -> None:
    """SL/TP must sit on the correct sides of the entry, with a sane stop distance."""
    sl, tps = signal.stop_loss, signal.take_profits
    if signal.side is Side.BUY:
        ok = sl < reference_price and all(tp > reference_price for tp in tps)
    else:
        ok = sl > reference_price and all(tp < reference_price for tp in tps)
    if not ok:
        raise SignalRejected(RejectReason.INVALID_GEOMETRY, f"{signal.side} ref={reference_price} sl={sl} tps={tps}")
    distance_pips = abs(reference_price - sl) / pip_size(signal.symbol)
    if distance_pips < limits.min_stop_distance_pips:
        raise SignalRejected(RejectReason.STOP_TOO_TIGHT, f"{distance_pips:.1f} pips")
    if distance_pips > limits.max_stop_distance_pips:
        raise SignalRejected(RejectReason.STOP_TOO_WIDE, f"{distance_pips:.1f} pips")


PriceLookup = Callable[[str, Side], float | None]


class SignalValidator:
    """Stateful gate: source allowlist, staleness, duplicates, then parse + geometry."""

    MAX_CLOCK_SKEW = timedelta(seconds=30)

    def __init__(
        self,
        source_allowlist: frozenset[int],
        symbol_allowlist: frozenset[str],
        limits: RiskLimits,
        max_tracked: int = 10_000,
    ) -> None:
        self._sources = source_allowlist
        self._symbols = symbol_allowlist
        self._limits = limits
        self._seen_messages: OrderedDict[tuple[int, int], datetime] = OrderedDict()
        self._seen_fingerprints: OrderedDict[str, datetime] = OrderedDict()
        self._max_tracked = max_tracked

    def _prune(self, now: datetime) -> None:
        horizon = now - timedelta(seconds=self._limits.dedupe_window_seconds)
        for store in (self._seen_messages, self._seen_fingerprints):
            while store and (next(iter(store.values())) < horizon or len(store) > self._max_tracked):
                store.popitem(last=False)

    def evaluate(self, msg: RawMessage, now: datetime, price_lookup: PriceLookup | None = None) -> SignalDecision:
        """``price_lookup(symbol, side)`` supplies a live price for market signals without an entry."""
        try:
            signal = self._evaluate(msg, now, price_lookup)
        except SignalRejected as exc:
            return SignalDecision(accepted=False, reasons=[exc.reason], detail=exc.detail)
        return SignalDecision(accepted=True, signal=signal)

    def _evaluate(self, msg: RawMessage, now: datetime, price_lookup: PriceLookup | None) -> TradeSignal:
        if msg.source_chat_id not in self._sources:
            raise SignalRejected(RejectReason.UNTRUSTED_SOURCE, str(msg.source_chat_id))
        age = now - msg.posted_at
        if age < -self.MAX_CLOCK_SKEW:
            raise SignalRejected(RejectReason.FUTURE_TIMESTAMP)
        if age.total_seconds() > self._limits.max_signal_age_seconds:
            raise SignalRejected(RejectReason.STALE, f"{age.total_seconds():.0f}s old")

        self._prune(now)
        key = (msg.source_chat_id, msg.message_id)
        if key in self._seen_messages:
            raise SignalRejected(RejectReason.DUPLICATE, "message already processed")

        signal = parse_signal(msg, self._symbols)
        if signal.fingerprint in self._seen_fingerprints:
            raise SignalRejected(RejectReason.DUPLICATE, "identical signal seen recently")

        ref = signal.entry
        if ref is None and price_lookup is not None:
            ref = price_lookup(signal.symbol, signal.side)
        if ref is None:
            raise SignalRejected(RejectReason.MISSING_ENTRY, "market signal without entry needs a live price")
        check_geometry(signal, ref, self._limits)

        # Record only after full validation so a rejected malformed message doesn't block a fix.
        self._seen_messages[key] = now
        self._seen_fingerprints[signal.fingerprint] = now
        return signal
