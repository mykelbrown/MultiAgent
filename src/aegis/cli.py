"""Owner-facing CLI. Approvals can only be created here (or later from the dashboard)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from aegis.core.audit import AuditLog
from aegis.core.config import Settings
from aegis.core.permissions import ApprovalStore, Capability


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
    sub.add_parser("audit-verify", help="verify audit hash chain").set_defaults(fn=cmd_audit_verify)
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
