# Project Aegis AI — Threat Model (v0.1, 2026-10-23)

Method: STRIDE per trust boundary, plus abuse cases specific to autonomous agents.
Review this document whenever a new integration, permission, or agent capability is added.

## 1. Assets (what we protect)

| Asset | Why it matters |
|---|---|
| Trading account balance & positions (MT5 broker) | Direct financial loss |
| Broker / Telegram / API credentials | Account takeover, unauthorized trading |
| Risk limits & kill-switch state | Bypass ⇒ unbounded loss |
| Audit log | Non-repudiation, incident forensics |
| User personal data (email, calendar, files) | Privacy, legal exposure |
| Platform source code & prompts | Self-modification ⇒ privilege escalation |
| Evaluation datasets | Poisoning ⇒ silently degraded agents |

## 2. Actors

- **Owner** (single human operator) — the only principal who can approve consequential actions.
- **Agents** (5 specialists) — untrusted-by-default executors; act only through the permission gate.
- **External content** — Telegram messages, web pages, MCP tool output, emails. *Always data, never instructions.*
- **Third-party tools** — MCP servers, PyPI/npm packages, APIs. Untrusted until reviewed and pinned.
- **Adversaries** — signal-channel spammers/scammers, prompt-injection authors, supply-chain attackers, local malware.

## 3. Trust boundaries

```
 [Telegram / Web / Email / MCP output]  --(B1: untrusted input)-->  [Parsers & validators]
 [Agents]  --(B2: capability request)-->  [Permission gate + Approval queue]  --> [Tools]
 [Risk engine]  --(B3: order)-->  [Broker adapter: Paper | MT5-demo | MT5-live(locked)]
 [Agent Factory]  --(B4: code)-->  [Isolated git branch]  --(human review)-->  [main]
 [Discovery]  --(B5: candidate tool)-->  [Quarantine/sandbox]  --(human approval)-->  [Registry: approved]
 [Secrets store]  --(B6)-->  [Process env only; never logs, never prompts]
```

## 4. STRIDE analysis

| # | Boundary | Threat | Mitigation (status) |
|---|---|---|---|
| T1 | B1 | **Spoofed signal** (fake channel, forwarded msg, stranger in group) | Source allowlist by numeric chat ID, not name (✅); intake-chat allowlist + trusted-user allowlist for groups; forwards attributed via server-set `forward_origin`; user/hidden-user forwards rejected; edits never executed (✅ Telegram bot) |
| T2 | B1 | **Malformed / ambiguous signal** ⇒ wrong trade | Strict schema; reject on missing SL, contradictory direction vs SL/TP geometry, multiple symbols, unknown symbol (slice 1 ✅) |
| T3 | B1 | **Replay / duplicate** signal ⇒ double exposure | Content hash + message-ID dedupe window (slice 1 ✅) |
| T4 | B1 | **Stale signal** executed at bad price | Max-age check; price-distance-from-entry check at execution (slice 1 ✅ age; price check in paper broker ✅) |
| T5 | B1 | **Prompt injection** in Telegram/web/email content steering an LLM agent | Deterministic regex parser for signals (no LLM in the execution path); LLM outputs never grant permissions; external content tagged as data (✅ design) |
| T6 | B2 | **Privilege escalation** — agent grants itself new tools/limits | Permissions are static config loaded at start; agents have no write path to policy; risk-limit *increase* requires owner approval token (slice 1 ✅) |
| T7 | B2 | **Confused deputy** — low-risk agent invokes high-risk tool | Per-agent capability allowlist, checked at the gate, deny by default (slice 1 ✅) |
| T8 | B3 | **Risk bypass** — order sent without checks | Broker adapters accept only `ApprovedOrder` objects minted by the risk engine (✅); live adapter refuses unless `AEGIS_TRADING_MODE=live` *and* a stored owner approval exists (✅ locked) |
| T9 | B3 | **Runaway loss** | Max lot, max open positions, per-symbol & total exposure, mandatory SL, max risk/trade %, daily loss limit, kill switch (slice 1 ✅) |
| T10 | B3 | **State drift** — local view ≠ broker | Reconciliation job compares local ledger to broker positions; mismatch ⇒ kill switch + alert (slice 1 ✅ paper; MT5 on Windows ⏳) |
| T11 | B4 | **Unreviewed self-modification** | Factory writes to `agent/*` branches only; no merge/deploy capability; CI + human review required (stage 4 ⏳) |
| T12 | B5 | **Supply-chain attack** via MCP/package | Read-only discovery; scoring; pin exact versions + hashes; license check; no auto-install; sandbox test (stage 2 ⏳) |
| T13 | B6 | **Secret leakage** in logs/prompts/repo | Secrets only from env/OS keyring; redaction filter on all log/audit writes; `.gitignore` for `.env`; pre-commit secret scan (slice 1 ✅ redaction) |
| T14 | All | **Repudiation / tampering with audit log** | Append-only SQLite with SHA-256 hash chain; `verify()` detects edits/deletions (slice 1 ✅) |
| T15 | All | **Data over-collection** (personal assistant) | Explicit folder allowlist, denylist for credential stores/cookies/keys, retention TTLs, redaction (stage 5 ⏳) |
| T16 | All | **DoS / cost blowout** (API loops) | Rate limits & budgets per agent (stage 3 ⏳) |
| T17 | Dashboard | **Unauthorized dashboard access / CSRF** | Bind 127.0.0.1 only; session token; CSRF on mutating routes; kill switch reachable (stage 6 ⏳) |

## 5. Abuse cases we explicitly refuse to support

- Bypassing broker, Telegram, OS, or platform security controls (e.g. scraping private channels with a user session in violation of ToS, defeating 2FA, disabling AV).
- Collecting passwords, session cookies, private keys, or browser profiles.
- Live trading, fund transfers, broker-permission changes, or risk-limit increases without an explicit, logged owner approval.
- Any claim or implication of guaranteed profitability.

## 6. Residual risks (accepted / open)

1. **MT5 terminal runs on Windows only**; this repo is developed on Linux. MT5 adapter is written against the documented API but **untested** until run on the owner's Windows demo account.
2. **Telegram:** Bot API chosen (D2). Third-party channels are covered only via the owner forwarding posts; a compromised owner Telegram account could inject signals (mitigated by risk limits and SL validation, not eliminated). Bot token stored in the OS user environment is readable by anything running as that Windows user.
3. Market gaps/slippage can exceed SL — daily loss limit is a soft bound on *realized+floating* P&L, not a guarantee.
4. Local-machine compromise defeats all in-process controls; mitigate with OS account hygiene and a dedicated Windows user for MT5.
