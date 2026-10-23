# Dependency register

Every dependency is pinned exactly in `pyproject.toml` and hash-locked in `uv.lock`.
Adding one requires an entry here. New MCP servers, executables, or anything touching private
accounts additionally require owner approval (see IMPLEMENTATION_PLAN.md, D4).

| Package | Version | Scope | License | Source / provenance | Why | Reviewed |
|---|---|---|---|---|---|---|
| pydantic | 2.13.5 | runtime | MIT | PyPI, pydantic/pydantic | Typed, validated schemas | 2026-10-23: already present in base image; widely used |
| pytest | 8.4.2 | dev | MIT | PyPI, pytest-dev/pytest | Tests | 2026-10-23 |
| ruff | 0.13.3 | dev | MIT | PyPI, astral-sh/ruff | Lint/format | 2026-10-23 |
| mypy | 1.18.2 | dev | MIT | PyPI, python/mypy | Static types | 2026-10-23 |
| MetaTrader5 | 5.0.6231 | optional `[mt5]`, Windows only | Proprietary (MetaQuotes) | PyPI JSON verified 2026-10-23: only `win_amd64` wheels | Official MT5 terminal bridge | **Not installed/tested yet**: needs owner's Windows host (D1) |

Rollback: `git revert` the commit that changed `pyproject.toml`/`uv.lock`, then `uv sync`.
