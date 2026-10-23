# Test log

| Date | Stage | Command | Result | Env |
|---|---|---|---|---|
| 2026-10-23 | 1 | `uv run pytest` | 72 passed | Linux container, Python 3.13.17 |
| 2026-10-23 | 1 | `uv run ruff check src tests` / `uv run mypy src` (strict) | clean / clean | same |
| 2026-10-23 | 1 | `uv run aegis demo` | 2 executed, 4 rejected as expected; reconciliation ok; audit chain ok | same |
| — | 4 | MT5 demo checklist | **not run** (needs Windows) | — |
