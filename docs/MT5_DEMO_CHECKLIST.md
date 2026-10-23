# MT5 demo adapter — manual verification (Windows, Stage 4)

The adapter in `src/aegis/trading/brokers/mt5.py` has only been tested against a fake module.
Do this on Windows with a **demo** account before relying on it. Nothing here enables live trading.

1. Install MetaTrader 5 from your broker, log in to a **demo** account, enable *Algo Trading*.
2. `uv sync --extra mt5` (installs MetaTrader5==5.0.6231).
3. `uv run python -c "import MetaTrader5 as m; print(m.initialize(), m.account_info().trade_mode)"` → expect `True 0` (0 = demo).
4. Log into a real account and repeat the adapter `connect()` → it must raise "not a DEMO account".
5. With demo: submit one 0.01-lot order through the pipeline; confirm in terminal that SL/TP are set and magic = 0xAE615.
6. Close it manually in the terminal; run reconciliation; confirm `ledger.closed_confirmed` in the audit log.
7. Open a manual trade in the terminal; run reconciliation → must trip the kill switch.
8. Trigger `emergency_stop` → all demo positions flattened.
Record results (date, build, broker server) in `docs/TEST_LOG.md`.
