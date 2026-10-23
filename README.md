# Project Aegis AI

A secure, modular multi-agent platform. **Paper/demo trading only in this release. No profitability is claimed or implied.**

- Threat model: [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)
- Staged plan and open decisions: [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
- Dependency register: [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)
- Test log: [`docs/TEST_LOG.md`](docs/TEST_LOG.md)

## Status

| Component | State |
|---|---|
| Audit log (hash chain, append-only), redaction, permission gate, approvals | ✅ implemented + tested |
| Telegram signal parser/validator (schema, stale, duplicate, contradictory, ambiguous) | ✅ implemented + tested (fixtures; no live Telegram yet) |
| Risk engine (sizing, caps, exposure, SL validation, daily loss, kill switch, signed orders) | ✅ implemented + tested |
| Paper broker + reconciliation + emergency flatten | ✅ implemented + tested |
| MT5 demo adapter | ⚠️ written; tested only against a fake module (needs Windows) |
| Discovery service (MCP registry + PyPI, scoring, provenance, approval-gated lifecycle, rollback) | ✅ read-only; `uv run aegis discover <query>` |
| Orchestrator, scheduler, notifications, other 4 agents, dashboard | ⏳ planned |

## Setup (Windows or Linux)

```powershell
# 1. Install uv: https://docs.astral.sh/uv/
# 2. From the repo root:
uv sync                      # creates .venv from the hash-locked uv.lock
uv run pytest                # run the test suite
uv run aegis demo            # paper-trading pipeline on sample signals (in memory)
uv run aegis audit-verify    # verify the audit log chain in ./var
uv run aegis approve trading.reset_kill_switch   # owner approval (interactive, single use)
# Windows + MT5 demo only (Stage 4):
uv sync --extra mt5
```

Configuration is via environment variables; see `.env.example`. Never put real credentials in the repo.

## Safety invariants

1. Trading mode defaults to `paper`; `live` is rejected by the risk engine in this release.
2. Brokers execute only orders HMAC-signed by the risk engine in the same process.
3. Every Telegram signal must have a stop loss, come from an allowlisted numeric chat ID, be fresh, unique, and unambiguous.
4. Increasing a risk limit, resetting the kill switch, sending external messages, installing tools: owner approval (single use, expiring).
5. No agent is allowlisted to move funds or merge to `main`.
6. Every decision is written to a tamper-evident audit log with secrets redacted.
