@echo off
chcp 65001 >nul 2>&1

echo [KiPIDA] Checking Python environment...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [KiPIDA] ERROR: Python is not installed or not in PATH.
    echo [KiPIDA] Please install Python 3.8+ from https://www.python.org/downloads/
    echo [KiPIDA] Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [KiPIDA] ERROR: pip is not available.
    echo [KiPIDA] Try running: python -m ensurepip --upgrade
    pause
    exit /b 1
)

echo [KiPIDA] Installing dependencies...
pip install -r "%~dp0requirements.txt" -q
if %errorlevel% neq 0 (
    echo [KiPIDA] Failed to install dependencies.
    pause
    exit /b 1
)

echo [KiPIDA] Starting service on http://localhost:5000 ...
python -m uvicorn main:app --reload --port 5000 --app-dir "%~dp0"
pause
