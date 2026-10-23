# Windows setup (paper + MT5 demo)

This has to be done on your PC: Claude's cloud session cannot reach it. To have Claude drive these steps
directly, run a Claude Code session on the PC (Claude Desktop app, or `claude remote-control` in the repo folder).

1. Install **MetaTrader 5** from your broker's site (or metatrader5.com) and open a **demo** account in it. Log in; enable **Algo Trading** (toolbar). Aegis never asks for or stores your MT5 password.
2. Install **git**, then clone: `git clone https://github.com/mykelbrown/MultiAgent.git; cd MultiAgent; git checkout claude/telegram-bot-mt5-setup`
3. Run `powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1`. It installs Python dependencies from the hash-locked `uv.lock` (offers to install `uv` via winget, asking first), runs the tests, and runs the **read-only** `aegis mt5-check`.
4. Expected `mt5-check` output: `is_demo: True`, ticks for EURUSD/GBPUSD/…, "No orders were sent."
5. Continue with `docs/TELEGRAM_BOT_SETUP.md`, then work through `docs/MT5_DEMO_CHECKLIST.md` and send me the results (paste output; redact account numbers if you like).

If MT5 is installed somewhere non-standard: `$env:AEGIS_MT5_TERMINAL_PATH = "D:\path\terminal64.exe"`.
