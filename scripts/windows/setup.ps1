<#
  Project Aegis AI - Windows setup (paper / MT5 DEMO only).
  Run from the repo root in PowerShell:   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
  What it does:  checks prerequisites, installs Python deps from the hash-locked uv.lock (incl. MetaTrader5),
                 runs the test suite, then a READ-ONLY MT5 check. It never places orders and never asks for passwords.
  What it does NOT do: download/install MetaTrader 5 (get it from your broker), log in to any account,
                 store secrets, or enable live trading.
#>
$ErrorActionPreference = "Stop"

function Confirm-Step($message) {
    $answer = Read-Host "$message [y/N]"
    return $answer -match '^(y|yes)$'
}

Write-Host "== 1. Prerequisites ==" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git not found. Install from https://git-scm.com/download/win and re-run." -ForegroundColor Yellow; exit 1
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv (Python package manager, astral-sh/uv, MIT/Apache-2.0) is not installed."
    if (Confirm-Step "Install uv now with winget (winget install --id=astral-sh.uv -e)?") {
        winget install --id=astral-sh.uv -e
        Write-Host "Close and reopen PowerShell, then re-run this script." -ForegroundColor Yellow; exit 0
    } else { Write-Host "See https://docs.astral.sh/uv/getting-started/installation/"; exit 1 }
}

Write-Host "== 2. Python environment (hash-locked) ==" -ForegroundColor Cyan
uv sync --extra mt5
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== 3. Test suite ==" -ForegroundColor Cyan
uv run pytest
if ($LASTEXITCODE -ne 0) { Write-Host "Tests failed - stop here and report the output." -ForegroundColor Red; exit $LASTEXITCODE }

Write-Host "== 4. MetaTrader 5 (read-only check) ==" -ForegroundColor Cyan
Write-Host "Before continuing: open MetaTrader 5, log in to a DEMO account, and enable 'Algo Trading'."
if (-not $env:AEGIS_MT5_TERMINAL_PATH) {
    $default = "C:\Program Files\MetaTrader 5\terminal64.exe"
    if (Test-Path $default) { $env:AEGIS_MT5_TERMINAL_PATH = $default }
}
if (Confirm-Step "Is the MT5 terminal running and logged in to a DEMO account?") {
    uv run aegis mt5-check
    switch ($LASTEXITCODE) {
        0 { Write-Host "MT5 demo check passed. No orders were sent." -ForegroundColor Green }
        3 { Write-Host "The terminal is logged in to a NON-demo account. Switch to demo." -ForegroundColor Red }
        default { Write-Host "MT5 check failed - see message above." -ForegroundColor Red }
    }
} else { Write-Host "Skipped MT5 check. Run 'uv run aegis mt5-check' later." }

Write-Host "== 5. Next ==" -ForegroundColor Cyan
Write-Host "Telegram bot setup: docs\TELEGRAM_BOT_SETUP.md, then 'uv run aegis telegram-check'."
