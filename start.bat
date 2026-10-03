@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   MESSAGE AUTOMATION — PRODUCTION LAUNCHER
echo ============================================================

cd /d "%~dp0"

:: 1. Detect Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10+ was not found in PATH.
    echo Please install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 2. Activate virtual environment if present
if exist ".venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment (.venv)...
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment (venv)...
    call venv\Scripts\activate.bat
)

:: 3. Check Playwright browser binaries
echo [INFO] Checking Playwright browser installation...
python -c "import playwright" >nul 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] Playwright library not found. Installing dependencies...
    pip install -r requirements.txt
)

:: 4. Ensure Chromium browser binary exists
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(headless=True).close(); p.stop()" >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installing Playwright Chromium browser binary...
    playwright install chromium
)

:: 5. Launch application with visible browser & live dashboard
echo [INFO] Starting Message Automation runtime...
echo [INFO] Live Dashboard: http://127.0.0.1:8080
echo [INFO] Visible Chrome will open for Instagram operator login.
echo ============================================================

python backend\main.py %*

if %errorlevel% neq 0 (
    echo [ERROR] Application terminated with error code %errorlevel%.
    pause
)
