@echo off
chcp 65001 >nul 2>&1
echo [KiPIDA] Checking Python dependencies...
pip install -r "%~dp0requirements.txt" -q
if %errorlevel% neq 0 (
    echo [KiPIDA] Failed to install dependencies. Please check your Python environment.
    pause
    exit /b 1
)
echo [KiPIDA] Starting service on http://localhost:5000 ...
python -m uvicorn main:app --reload --port 5000 --app-dir "%~dp0"
pause
