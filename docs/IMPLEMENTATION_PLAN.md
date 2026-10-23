# Project Aegis AI — Staged Implementation Plan (v0.1, 2026-10-23)

## 0. Environment findings (inspected, not assumed)

| Item | Finding | Consequence |
|---|---|---|
| Host | Ubuntu 24.04 cloud container (Linux), 4 CPU / 15 GB | Target is Windows: code must be cross-platform; Windows-only parts isolated behind adapters |
| Existing work | `/home/user` empty, no git repo, no GitHub repos visible to session | Greenfield; nothing to overwrite |
| Toolchain | Python 3.13.17, uv 0.11.32, Node 22, Docker 29, git, gh | **Python 3.13 + uv** (lockfile with hashes) |
| Installed libs | pydantic 2.13.5, cryptography, uvicorn | pydantic for typed schemas |
| MetaTrader5 (PyPI) | v5.0.6231, **win_amd64 wheels only** (verified via PyPI JSON) | Optional extra `[mt5]`, platform-marked; adapter import-guarded |
| MCP registry | `registry.modelcontextprotocol.io/v0/servers?search=` reachable, returns JSON | Discovery service can start with this read-only source |
| MCP servers in this session | Alpha Vantage, FMP (market data), Gmail/Calendar/Drive, others | Candidate data sources for agents 1 & 5 — **not wired in without owner approval** |
| Network | Egress via policy proxy with CA bundle | HTTP clients must honour `SSL_CERT_FILE`/proxy env |

## 1. Architecture

Modular monolith (single Python package, clear module boundaries), so it is easy to run on one
Windows PC and can be split into processes later. All cross-module calls go through typed
interfaces (`Protocol`s + pydantic models).

```
aegis/
  core/        config, audit (hash chain), permissions+approvals, secrets, redaction, events
  trading/     signal schema+parser+validator, risk engine, kill switch, brokers (paper, mt5), reconciliation
  agents/      forex_research, telegram_intake, prompt_eval, agent_factory, assistant
  discovery/   sources (MCP registry, PyPI, GitHub), scoring, provenance, quarantine
  scheduler/   jobs (reconciliation, discovery refresh, eval runs)
  notify/      notification sinks (console, file; Telegram bot later)
  evals/       versioned datasets, runners, regression reports
  dashboard/   local web UI (127.0.0.1), read-mostly + kill switch
```

Execution path for a Telegram signal (no LLM in the loop):

```
raw message ─► SourceAllowlist ─► Parser ─► SignalValidator (schema, staleness, dedupe,
contradiction) ─► RiskEngine (limits, SL, exposure, daily loss, kill switch) ─► ApprovedOrder
─► Broker[paper|mt5-demo] ─► Ledger ─► Reconciler ─► AuditLog (every step)
```

## 2. Stages (each = vertical slice → test → review → summary)

| Stage | Scope | Exit criteria |
|---|---|---|
| **0** | Inspection, threat model, plan, repo skeleton, locked deps | Docs committed ✅ |
| **1** | Core (config, audit chain, permissions, redaction) + Telegram signal parser/validator + risk engine + kill switch + paper broker + reconciliation + CLI demo | Unit tests green; end-to-end paper flow from a fixture message |
| **2** | Discovery service: MCP-registry + PyPI read-only sources, scoring rubric, provenance records, quarantine list; one harmless integration test | Live read-only query works (opt-in network test); no installs |
| **3** | Orchestrator + scheduler + notifications + approval queue (CLI) + per-agent budgets | Scheduled reconciliation & discovery jobs run in tests |
| **4** | MT5 adapter (demo) on Windows: `initialize`, `account_info` (assert demo trade_mode), `order_check` before `order_send`, positions sync | **Requires owner's Windows machine + demo account**; manual test checklist |
| **5** | Telegram live intake (method per D2), demo-mode only | Fixture + live demo channel test |
| **6** | Prompt Engineering & Eval agent: versioned datasets, deterministic graders + LLM-judge, regression report | Regression detected on a seeded bad prompt |
| **7** | Agent Factory: generates code/tests/docs on `agent/*` branches, runs tests, opens draft PR; no merge rights | Produces a branch + passing tests; cannot touch main |
| **8** | Personal Assistant: folder allowlist, research/draft/organize, approval for external actions (send email, create events) | Denylist tests; approval required for sends |
| **9** | Forex Research agent: data via approved source, backtester, research reports (no profitability claims) | Backtest reproducible from pinned data |
| **10** | Dashboard (FastAPI, 127.0.0.1, token auth): audit view, positions, approvals, kill switch | Auth + CSRF tests |

## 3. Owner decisions (updated 2026-10-23)

- **D1 – Windows host for MT5: DECIDED.** Owner will run MT5 on their Windows PC with a demo account. The cloud session cannot reach that PC, so setup is via `scripts/windows/setup.ps1` (read-only `aegis mt5-check`), or a local Claude Code session on the PC.
- **D2 – Telegram ingestion: DECIDED → Bot API.** Bot in channels the owner controls, plus forwarding into a private intake group. No personal-account (MTProto) access. Implemented in `src/aegis/telegram/`.
- **D3 – LLM provider: OPEN.** Default recommendation: Anthropic Claude via the official SDK, behind a provider interface, key from env/Credential Manager. To be confirmed by owner before Stage 6.
- **D4 – Dependency policy: DECIDED → safer reading.** Well-known libraries pinned in `uv.lock` may be added and recorded in `docs/DEPENDENCIES.md`; new MCP servers, executables, private-account access and permission grants require owner approval.

## 4. Policies (enforced in code where marked)

- Trading mode default `paper`; `demo` allowed; `live` locked behind env flag **and** owner approval record (code ✅ stage 1).
- Risk-limit changes: decreases allowed, increases need approval (code ✅).
- No secrets in repo; `.env` ignored; `.env.example` contains names only.
- Agents improve via proposals → eval → human review → merge. No silent self-modification.
