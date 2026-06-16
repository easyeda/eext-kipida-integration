#!/usr/bin/env bash
# KiPIDA service launcher for macOS.
# Downloads KiPIDA core + service files if missing, checks the Python
# environment, installs deps, then runs main.py.
set -u

# Directory containing this script (resolves symlinks).
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"

RAW_KIPIDA="https://raw.githubusercontent.com/kbralten/KiPIDA/main"
RAW_SERVICE="https://raw.githubusercontent.com/easyeda/eext-kipida-integration/main/kipida-service"

echo "[KiPIDA] Checking KiPIDA path..."
KIPIDA_PATH="${KIPIDA_PATH:-$SCRIPT_DIR/KiPIDA}"
mkdir -p "$KIPIDA_PATH"

download() {
  # download <url> <dest> <label>
  local url="$1" dest="$2" label="$3"
  if [ ! -f "$dest" ]; then
    echo "[KiPIDA] $label not found, downloading from GitHub..."
    curl -fL -o "$dest" "$url"
    if [ ! -f "$dest" ]; then
      echo "[KiPIDA] ERROR: Failed to download $label"
      echo "[KiPIDA] Please manually download from https://github.com/kbralten/KiPIDA"
      read -rp "Press Enter to exit..."
      exit 1
    fi
    echo "[KiPIDA] $label downloaded."
  fi
}

download "$RAW_KIPIDA/mesh.py"   "$KIPIDA_PATH/mesh.py"   "mesh.py"
download "$RAW_KIPIDA/solver.py" "$KIPIDA_PATH/solver.py" "solver.py"

echo "[KiPIDA] Using KiPIDA at: $KIPIDA_PATH"
export KIPIDA_PATH

echo "[KiPIDA] Checking service files..."
download "$RAW_SERVICE/main.py"          "$SCRIPT_DIR/main.py"          "main.py"
download "$RAW_SERVICE/gerber_pour.py"   "$SCRIPT_DIR/gerber_pour.py"   "gerber_pour.py"
download "$RAW_SERVICE/requirements.txt" "$SCRIPT_DIR/requirements.txt" "requirements.txt"

echo "[KiPIDA] Checking Python environment..."

# macOS ships only python3 (the bare 'python' was removed in recent versions).
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
  PYTHON="python"
fi

if [ -z "$PYTHON" ]; then
  echo "[KiPIDA] ERROR: Python 3 is not installed or not in PATH."
  echo "[KiPIDA] Install via Homebrew:  brew install python"
  echo "[KiPIDA] Or download from:      https://www.python.org/downloads/macos/"
  read -rp "Press Enter to exit..."
  exit 1
fi

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
  echo "[KiPIDA] ERROR: pip is not available."
  echo "[KiPIDA] Try running: $PYTHON -m ensurepip --upgrade"
  read -rp "Press Enter to exit..."
  exit 1
fi

echo "[KiPIDA] Installing dependencies..."
"$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" -q
if [ $? -ne 0 ]; then
  echo "[KiPIDA] Failed to install dependencies."
  read -rp "Press Enter to exit..."
  exit 1
fi

echo "[KiPIDA] Starting service (auto-detecting available port)..."
cd "$SCRIPT_DIR"
"$PYTHON" main.py
read -rp "Press Enter to exit..."
