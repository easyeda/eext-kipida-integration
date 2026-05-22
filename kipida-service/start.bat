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
    curl -L -o "%KIPIDA_PATH%\mesh.py" "https://raw.githubusercontent.com/kbralten/KiPIDA/main/mesh.py"
    if not exist "%KIPIDA_PATH%\mesh.py" (
        echo [KiPIDA] ERROR: Failed to download mesh.py
        echo [KiPIDA] Please manually download from https://github.com/kbralten/KiPIDA
        pause
        exit /b 1
    )
    echo [KiPIDA] mesh.py downloaded.
)

if not exist "%KIPIDA_PATH%\solver.py" (
    echo [KiPIDA] solver.py not found, downloading from GitHub...
    curl -L -o "%KIPIDA_PATH%\solver.py" "https://raw.githubusercontent.com/kbralten/KiPIDA/main/solver.py"
    if not exist "%KIPIDA_PATH%\solver.py" (
        echo [KiPIDA] ERROR: Failed to download solver.py
        echo [KiPIDA] Please manually download from https://github.com/kbralten/KiPIDA
        pause
        exit /b 1
    )
    echo [KiPIDA] solver.py downloaded.
)

echo [KiPIDA] Using KiPIDA at: %KIPIDA_PATH%

echo [KiPIDA] Checking service files...
if not exist "%~dp0main.py" (
    echo [KiPIDA] main.py not found, downloading...
    curl -L -o "%~dp0main.py" "https://raw.githubusercontent.com/easyeda/eext-kipida-integration/main/kipida-service/main.py"
    if not exist "%~dp0main.py" (
        echo [KiPIDA] ERROR: Failed to download main.py
        pause
        exit /b 1
    )
    echo [KiPIDA] main.py downloaded.
)

if not exist "%~dp0requirements.txt" (
    echo [KiPIDA] requirements.txt not found, downloading...
    curl -L -o "%~dp0requirements.txt" "https://raw.githubusercontent.com/easyeda/eext-kipida-integration/main/kipida-service/requirements.txt"
    if not exist "%~dp0requirements.txt" (
        echo [KiPIDA] ERROR: Failed to download requirements.txt
        pause
        exit /b 1
    )
    echo [KiPIDA] requirements.txt downloaded.
)

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

echo [KiPIDA] Starting service (auto-detecting available port)...
cd /d "%~dp0"
python main.py
pause
