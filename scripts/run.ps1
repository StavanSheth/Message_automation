# PowerShell launcher for Message Automation
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot\..

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  MESSAGE AUTOMATION — POWERSHELL RUNNER" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Detect Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "Python 3.10+ was not found in PATH."
    exit 1
}

# 2. Activate virtual environment if present
if (Test-Path ".venv\Scripts\Activate.ps1") {
    Write-Host "[INFO] Activating virtual environment (.venv)..." -ForegroundColor Green
    & .venv\Scripts\Activate.ps1
} elseif (Test-Path "venv\Scripts\Activate.ps1") {
    Write-Host "[INFO] Activating virtual environment (venv)..." -ForegroundColor Green
    & venv\Scripts\Activate.ps1
}

# 3. Ensure Playwright browser binary
try {
    python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(headless=True).close(); p.stop()" 2>$null
} catch {
    Write-Host "[INFO] Installing Playwright Chromium browser binary..." -ForegroundColor Yellow
    playwright install chromium
}

# 4. Start production application
Write-Host "[INFO] Starting Message Automation production app..." -ForegroundColor Green
python backend/main.py @args
