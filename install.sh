#!/usr/bin/env bash
# ==============================================================================
# Bybit V5 Autonomous Engine - Production Bootstrap Installer
# Zero-touch setup for Debian/Ubuntu/GCP Compute Engine instances.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo -e "${CYAN}${BOLD}"
echo "======================================================================"
echo "    BYBIT V5 AUTONOMOUS QUANTITATIVE TRADING ENGINE INSTALLER        "
echo "======================================================================"
echo -e "${RESET}"

# 1. Check OS & Architecture
OS="$(uname -s)"
ARCH="$(uname -m)"
echo -e "${YELLOW}[1/5] Verifying Operating System...${RESET}"
echo "Detected OS: $OS ($ARCH)"

if [ "$OS" != "Linux" ]; then
    echo -e "${YELLOW}Warning: Non-Linux OS detected. Target production environment is Linux (Debian/Ubuntu on GCP).${RESET}"
fi

# 2. Check Python 3.11+
echo -e "\n${YELLOW}[2/5] Checking Python Runtime...${RESET}"
PYTHON_BIN=""

for cmd in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PY_VER="$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        MAJOR="$($cmd -c 'import sys; print(sys.version_info.major)')"
        MINOR="$($cmd -c 'import sys; print(sys.version_info.minor)')"
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
            PYTHON_BIN="$(command -v "$cmd")"
            echo -e "${GREEN}✓ Found compatible Python $PY_VER at $PYTHON_BIN${RESET}"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}Error: Python 3.11 or newer is required.${RESET}"
    if command -v apt-get >/dev/null 2>&1; then
        echo -e "${CYAN}Attempting to install Python via apt...${RESET}"
        sudo apt-get update && sudo apt-get install -y python3 python3-venv git curl
        PYTHON_BIN="$(command -v python3)"
    else
        echo "Please install Python 3.11+ using your system package manager."
        exit 1
    fi
fi

# 3. Create Virtual Environment
echo -e "\n${YELLOW}[3/5] Setting up Virtual Environment (.venv)...${RESET}"

if [ ! -d ".venv" ]; then
    if ! "$PYTHON_BIN" -m venv .venv 2>/dev/null; then
        echo -e "${YELLOW}Standard venv creation failed (ensurepip absent). Falling back to --without-pip bootstrap...${RESET}"
        "$PYTHON_BIN" -m venv --without-pip .venv
        curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        .venv/bin/python3 /tmp/get-pip.py --quiet
        rm -f /tmp/get-pip.py
    fi
    echo -e "${GREEN}✓ Virtual environment created successfully.${RESET}"
else
    echo -e "${GREEN}✓ Virtual environment already exists.${RESET}"
fi

# 4. Install Dependencies
echo -e "\n${YELLOW}[4/5] Installing and upgrading dependencies...${RESET}"
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo -e "${GREEN}✓ All dependencies verified and installed.${RESET}"

# Ensure manage.sh is executable
if [ -f "manage.sh" ]; then
    chmod +x manage.sh
fi
chmod +x installer.py

# 5. Launch Interactive Setup Wizard
echo -e "\n${YELLOW}[5/5] Launching Configuration Wizard...${RESET}"
.venv/bin/python3 installer.py "$@"

echo -e "\n${GREEN}${BOLD}Installation and setup completed!${RESET}\n"
