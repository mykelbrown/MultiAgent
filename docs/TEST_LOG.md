# Test log

| Date | Stage | Command | Result | Env |
|---|---|---|---|---|
| 2026-10-23 | 1 | `uv run pytest` | 72 passed | Linux container, Python 3.13.17 |
| 2026-10-23 | 1 | `uv run ruff check src tests` / `uv run mypy src` (strict) | clean / clean | same |
| 2026-10-23 | 1 | `uv run aegis demo` | 2 executed, 4 rejected as expected; reconciliation ok; audit chain ok | same |
| — | 4 | MT5 demo checklist | **not run** (needs Windows) | — |
| 2026-10-23 | 2 | `uv run pytest` | 80 passed, 1 skipped (network test opt-in) | Linux container |
| 2026-10-23 | 2 | `AEGIS_NETWORK_TESTS=1 uv run pytest` | 81 passed (live read-only MCP registry + PyPI GET) | same, via egress proxy |
| 2026-10-23 | 2 | `aegis discover telegram` / `aegis discover MetaTrader5,python-telegram-bot,telethon --sources pypi` | Ranked candidates with flags; all left as `discovered`; nothing installed | same |
