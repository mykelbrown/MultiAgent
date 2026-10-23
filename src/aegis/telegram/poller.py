"""Long-polling loop: Bot API -> mapper -> TelegramIntakeAgent.

Delivery is deliberately *at-most-once*: the next offset is persisted before an update is
handled, so a crash can drop a signal but can never replay one into a duplicate trade.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aegis.agents.telegram_intake import IntakeOutcome, TelegramIntakeAgent
from aegis.core.audit import AuditLog
from aegis.telegram.bot_api import BotApi, TelegramApiError
from aegis.telegram.mapper import TelegramIntakeConfig, map_update


class OffsetStore:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._offset: int | None = None
        if path and path.exists():
            self._offset = json.loads(path.read_text()).get("offset")

    @property
    def offset(self) -> int | None:
        return self._offset

    def save(self, offset: int) -> None:
        self._offset = offset
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"offset": offset}))
            tmp.replace(self._path)


class TelegramPoller:
    AGENT = "telegram_intake"

    def __init__(
        self,
        api: BotApi,
        cfg: TelegramIntakeConfig,
        agent: TelegramIntakeAgent,
        offsets: OffsetStore,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._cfg = cfg
        self._agent = agent
        self._offsets = offsets
        self._audit = audit
        self._clock = clock

    def poll_once(self, timeout: int = 25) -> list[IntakeOutcome]:
        updates = self._api.get_updates(self._offsets.offset, timeout=timeout)
        outcomes: list[IntakeOutcome] = []
        for update in sorted(updates, key=lambda u: int(u["update_id"])):
            self._offsets.save(int(update["update_id"]) + 1)  # commit first: at-most-once
            mapped = map_update(update, self._cfg)
            if mapped.message is None:
                self._audit.append(
                    self.AGENT,
                    "telegram.update_rejected",
                    {
                        "update_id": mapped.update_id,
                        "reason": mapped.reject,
                        "detail": mapped.detail,
                        "chat": mapped.delivered_via,
                    },
                )
                continue
            outcomes.append(self._agent.handle(mapped.message, self._clock()))
        return outcomes

    def run_forever(self, on_outcome: Callable[[IntakeOutcome], None] | None = None) -> None:
        backoff = 1.0
        while True:
            try:
                for outcome in self.poll_once():
                    if on_outcome:
                        on_outcome(outcome)
                backoff = 1.0
            except TelegramApiError as exc:
                self._audit.append(self.AGENT, "telegram.api_error", {"error": str(exc)})
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
