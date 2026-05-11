@echo off
chcp 65001 >nul 2>&1

echo [KiPIDA] Checking KiPIDA path...
if not defined KIPIDA_PATH (
    set "KIPIDA_PATH=%~dp0KiPIDA"
)
if not exist "%KIPIDA_PATH%" (
    mkdir "%KIPIDA_PATH%"
)

if not exist "%KIPIDA_PATH%\mesh.py" (
    echo [KiPIDA] mesh.py not found, downloading from GitHub...
    curl -sL -o "%KIPIDA_PATH%\mesh.py" "https://raw.githubusercontent.com/kbralten/KiPIDA/main/mesh.py"
    if %errorlevel% neq 0 (
        echo [KiPIDA] ERROR: Failed to download mesh.py
        echo [KiPIDA] Please manually download from https://github.com/kbralten/KiPIDA
        pause
        exit /b 1
    )
    echo [KiPIDA] mesh.py downloaded.
)

if not exist "%KIPIDA_PATH%\solver.py" (
    echo [KiPIDA] solver.py not found, downloading from GitHub...
    curl -sL -o "%KIPIDA_PATH%\solver.py" "https://raw.githubusercontent.com/kbralten/KiPIDA/main/solver.py"
    if %errorlevel% neq 0 (
        echo [KiPIDA] ERROR: Failed to download solver.py
        echo [KiPIDA] Please manually download from https://github.com/kbralten/KiPIDA
        pause
        exit /b 1
    )
    echo [KiPIDA] solver.py downloaded.
)

echo [KiPIDA] Using KiPIDA at: %KIPIDA_PATH%

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
