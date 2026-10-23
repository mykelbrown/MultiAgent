"""Owner-facing CLI. Approvals can only be created here (or later from the dashboard)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aegis.core.audit import AuditLog
from aegis.core.config import Settings
from aegis.core.permissions import ApprovalStore, Capability

if TYPE_CHECKING:
    from aegis.telegram.bot_api import BotApi


def _paths(settings: Settings) -> tuple[AuditLog, ApprovalStore]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(settings.data_dir / "audit.db")
    return audit, ApprovalStore(settings.data_dir / "approvals.json", audit)


def cmd_demo(_: argparse.Namespace) -> int:
    """Run the Telegram -> risk -> paper-broker pipeline on fixture messages (in-memory)."""
    from aegis.agents.telegram_intake import TelegramIntakeAgent
    from aegis.core.permissions import PermissionGate
    from aegis.trading.brokers.paper import PaperBroker
    from aegis.trading.reconcile import Ledger, Reconciler
    from aegis.trading.risk import KillSwitch, OrderSigner, RiskEngine
    from aegis.trading.signals import RawMessage, SignalValidator

    source = -100999
    settings = Settings(telegram_source_allowlist=frozenset({source}))
    audit = AuditLog()
    approvals = ApprovalStore(audit=audit)
    kill = KillSwitch(None, audit)
    signer = OrderSigner()
    broker = PaperBroker(signer, audit)
    now = datetime.now(UTC)
    broker.set_quote("EURUSD", 1.08500, 1.08510, now)
    broker.set_quote("GBPUSD", 1.30000, 1.30015, now)
    ledger = Ledger()
    agent = TelegramIntakeAgent(
        PermissionGate(audit, approvals),
        SignalValidator(settings.telegram_source_allowlist, settings.symbol_allowlist, settings.risk),
        RiskEngine(settings.risk, settings.symbol_allowlist, kill, audit, signer),
        broker,
        ledger,
        kill,
        audit,
    )
    samples = [
        (source, "BUY EURUSD 1.0851 SL 1.0820 TP 1.0900", now),
        (source, "BUY EURUSD 1.0851 SL 1.0820 TP 1.0900", now),  # duplicate content
        (source, "SELL GBPUSD SL 1.3030 TP 1.2950", now),  # market, no entry
        (source, "BUY EURUSD 1.0851 TP 1.0900", now),  # no SL
        (12345, "BUY EURUSD 1.0851 SL 1.0820", now),  # untrusted source
        (source, "SELL GBPUSD 1.3000 SL 1.3030", now - timedelta(minutes=10)),  # stale
    ]
    for i, (src, text, ts) in enumerate(samples, start=1):
        out = agent.handle(RawMessage(source_chat_id=src, message_id=i, text=text, posted_at=ts), now)
        status = f"EXECUTED ticket={out.ticket}" if out.accepted else f"REJECTED at {out.stage}: {out.reasons}"
        print(f"[{i}] {text!r:45} -> {status}")
    print("reconciliation:", Reconciler(ledger, broker, kill, audit).run().ok)
    print("account:", broker.account().model_dump())
    print("audit chain:", audit.verify())
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    _, approvals = _paths(settings)
    cap = Capability(args.capability)
    print(f"You are approving: {cap.value} (expires in {args.minutes} min, single use)")
    if input("Type APPROVE to confirm: ").strip() != "APPROVE":
        print("Cancelled.")
        return 1
    a = approvals.grant(cap, approved_by="owner-cli", ttl=timedelta(minutes=args.minutes))
    print(f"Approval {a.id} recorded.")
    return 0


def cmd_audit_verify(_: argparse.Namespace) -> int:
    audit, _approvals = _paths(Settings.from_env())
    ok, message = audit.verify()
    print(message)
    return 0 if ok else 2


def cmd_discover(args: argparse.Namespace) -> int:
    """Read-only discovery: queries the MCP registry and PyPI metadata. Installs nothing."""
    from aegis.core.permissions import PermissionGate
    from aegis.discovery.registry import DiscoveryService, ToolRegistry
    from aegis.discovery.sources import McpRegistrySource, PyPISource, Source

    settings = Settings.from_env()
    audit, approvals = _paths(settings)
    registry = ToolRegistry(settings.data_dir / "tool_registry.json", audit, approvals)
    sources: dict[str, Source] = {"mcp": McpRegistrySource(), "pypi": PyPISource()}
    svc = DiscoveryService([sources[s] for s in args.sources], registry, PermissionGate(audit, approvals), audit)
    for s in svc.discover(args.query, limit=args.limit, platform=args.platform):
        c = s.candidate
        print(f"{s.score.total:4.2f}  {c.kind:12} {c.name}  v{c.version}  license={c.license}  repo={c.repository_url}")
        for flag in s.score.flags:
            print(f"        ! {flag}")
    print("\nAll candidates recorded as 'discovered'. Nothing was installed.")
    return 0


def cmd_mt5_check(_: argparse.Namespace) -> int:
    """Read-only MT5 check (Windows): connects to the running terminal, reports account type and a tick."""
    from aegis.trading.brokers.base import BrokerError
    from aegis.trading.brokers.mt5 import MT5ReadOnly

    try:
        mt5 = MT5ReadOnly()
    except BrokerError as exc:
        print(f"FAIL: {exc}")
        return 2
    try:
        summary = mt5.account_summary()
        print("account:", summary)
        for symbol in sorted(Settings.from_env().symbol_allowlist):
            try:
                q = mt5.quote(symbol)
                print(f"tick {symbol}: bid={q.bid} ask={q.ask} at {q.ts.isoformat()}")
            except BrokerError as exc:
                print(f"tick {symbol}: unavailable ({exc})")
        if not summary["is_demo"]:
            print("WARNING: logged-in account is NOT demo. Aegis will refuse to place orders on it.")
            return 3
        print("OK: demo account reachable. No orders were sent.")
        return 0
    finally:
        mt5.shutdown()


def _bot_api() -> BotApi:
    from aegis.core.secrets import get_secret
    from aegis.telegram.bot_api import BotApi, make_transport

    return BotApi(make_transport(get_secret("AEGIS_TELEGRAM_BOT_TOKEN")))


def cmd_telegram_check(_: argparse.Namespace) -> int:
    """Read-only: bot identity, webhook/privacy status, and chat/user IDs seen in *pending* updates
    (pending updates are not confirmed, so nothing is consumed)."""
    from aegis.core.secrets import MissingSecret
    from aegis.telegram.bot_api import TelegramApiError

    try:
        api = _bot_api()
        me = api.get_me()
        hook = api.get_webhook_info()
        pending = api.get_updates(offset=None, timeout=0)
    except (MissingSecret, TelegramApiError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"bot: @{me.get('username')} (id {me.get('id')})")
    print(f"reads all group messages (privacy mode off): {me.get('can_read_all_group_messages')}")
    if hook.get("url"):
        print("WARNING: a webhook is set; getUpdates will not work until it is removed (deleteWebhook).")
    seen: dict[int, str] = {}
    users: dict[int, str] = {}
    for u in pending:
        msg: dict[str, Any] = next(
            (u[k] for k in ("channel_post", "message", "edited_message", "edited_channel_post") if k in u), {}
        )
        chat = msg.get("chat", {})
        seen[chat.get("id", 0)] = f"{chat.get('type')} '{chat.get('title') or chat.get('username') or ''}'"
        if sender := msg.get("from"):
            users[sender["id"]] = sender.get("username") or sender.get("first_name", "")
        origin = msg.get("forward_origin") or {}
        if origin.get("type") == "channel":
            seen[origin["chat"]["id"]] = f"channel '{origin['chat'].get('title', '')}' (forward origin)"
    print("chats seen in pending updates (use for AEGIS_TELEGRAM_INTAKE_CHATS / AEGIS_TELEGRAM_SOURCES):")
    for cid, desc in seen.items():
        print(f"  {cid}: {desc}")
    print("senders seen (use for AEGIS_TELEGRAM_TRUSTED_USERS):")
    for user_id, name in users.items():
        print(f"  {user_id}: {name}")
    return 0


def cmd_telegram_run(args: argparse.Namespace) -> int:
    """Run the Telegram bot intake. Paper (MT5 live prices) or MT5 demo orders. Never live."""
    from aegis.agents.telegram_intake import TelegramIntakeAgent
    from aegis.core.permissions import PermissionGate
    from aegis.telegram.mapper import TelegramIntakeConfig
    from aegis.telegram.poller import OffsetStore, TelegramPoller
    from aegis.trading.brokers.base import Broker
    from aegis.trading.brokers.mt5 import MT5DemoBroker, MT5ReadOnly
    from aegis.trading.brokers.paper import PaperBroker
    from aegis.trading.reconcile import Ledger, Reconciler
    from aegis.trading.risk import KillSwitch, OrderSigner, RiskEngine
    from aegis.trading.signals import SignalValidator

    settings = Settings.from_env()
    if not (settings.telegram_intake_chats and settings.telegram_source_allowlist):
        print("Set AEGIS_TELEGRAM_INTAKE_CHATS and AEGIS_TELEGRAM_SOURCES first (see `aegis telegram-check`).")
        return 2
    audit, approvals = _paths(settings)
    kill = KillSwitch(settings.data_dir / "kill.json", audit)
    signer = OrderSigner()
    broker: Broker
    if args.broker == "mt5-demo":
        demo = MT5DemoBroker(signer, audit)
        demo.connect()  # refuses non-demo accounts
        broker = demo
    else:
        feed = MT5ReadOnly()
        broker = PaperBroker(signer, audit, balance=args.paper_balance, quote_feed=feed.quote)
    ledger = Ledger()
    agent = TelegramIntakeAgent(
        PermissionGate(audit, approvals),
        SignalValidator(settings.telegram_source_allowlist, settings.symbol_allowlist, settings.risk),
        RiskEngine(settings.risk, settings.symbol_allowlist, kill, audit, signer),
        broker,
        ledger,
        kill,
        audit,
    )
    api = _bot_api()
    poller = TelegramPoller(
        api,
        TelegramIntakeConfig(
            intake_chats=settings.telegram_intake_chats, trusted_users=settings.telegram_trusted_users
        ),
        agent,
        OffsetStore(settings.data_dir / "telegram_offset.json"),
        audit,
    )
    reconciler = Reconciler(ledger, broker, kill, audit)
    state = "TRIPPED" if kill.tripped else "armed"
    print(f"Listening via Telegram bot -> {args.broker}. Kill switch: {state}. Ctrl+C to stop.")

    def report(outcome: object) -> None:
        print(outcome)
        reconciler.run()

    try:
        poller.run_forever(on_outcome=report)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aegis")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("demo", help="paper-trading pipeline demo").set_defaults(fn=cmd_demo)
    p = sub.add_parser("approve", help="owner approval for a consequential capability")
    p.add_argument("capability", choices=[c.value for c in Capability])
    p.add_argument("--minutes", type=int, default=15)
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("discover", help="read-only tool discovery (MCP registry, PyPI)")
    p.add_argument("query", help="search text (mcp) / comma-separated package names (pypi)")
    p.add_argument("--sources", nargs="+", choices=["mcp", "pypi"], default=["mcp"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--platform", default="win_amd64")
    p.set_defaults(fn=cmd_discover)
    sub.add_parser("mt5-check", help="read-only MT5 terminal check (Windows)").set_defaults(fn=cmd_mt5_check)
    sub.add_parser("telegram-check", help="read-only Telegram bot check").set_defaults(fn=cmd_telegram_check)
    p = sub.add_parser("telegram-run", help="run Telegram signal intake (paper or MT5 demo)")
    p.add_argument("--broker", choices=["paper", "mt5-demo"], default="paper")
    p.add_argument("--paper-balance", type=float, default=10_000.0)
    p.set_defaults(fn=cmd_telegram_run)
    sub.add_parser("audit-verify", help="verify audit hash chain").set_defaults(fn=cmd_audit_verify)
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
