#!/usr/bin/env bash
# ==============================================================================
# Bybit V5 Autonomous Engine - Operational Management Utility
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

SERVICE_NAME="bybit-engine.service"
PYTHON=".venv/bin/python3"

# Detect whether service is system-wide or user-level
SYSTEMCTL_CMD="systemctl"
if systemctl --user is-active "$SERVICE_NAME" >/dev/null 2>&1 || [ -f "$HOME/.config/systemd/user/$SERVICE_NAME" ]; then
    SYSTEMCTL_CMD="systemctl --user"
elif [ "$EUID" -ne 0 ]; then
    # Try sudo if available, else user systemctl
    if sudo -n true 2>/dev/null; then
        SYSTEMCTL_CMD="sudo systemctl"
    else
        SYSTEMCTL_CMD="systemctl --user"
    fi
fi

usage() {
    echo -e "${CYAN}${BOLD}Bybit V5 Autonomous Engine - Control Utility${RESET}"
    echo "Usage: ./manage.sh [command]"
    echo ""
    echo "Available Commands:"
    echo "  status   - Display live PnL, active positions, win rate, and circuit breakers"
    echo "  verify   - Deep diagnostic test of Bybit API keys, IP whitelist & connectivity"
    echo "  web      - Launch the Web Admin Executive Dashboard & Installer (port 8080)"
    echo "  logs     - Stream live real-time engine execution logs"
    echo "  panic    - EMERGENCY: Cancel all open orders and market close all positions"
    echo "  update   - Pull latest git updates, sync virtualenv, and restart daemon"
    echo "  start    - Start background daemon"
    echo "  stop     - Stop background daemon"
    echo "  restart  - Cleanly restart background daemon"
    echo ""
}

COMMAND="${1:-status}"

case "$COMMAND" in
    status)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        $PYTHON manage_cli.py status
        ;;

    verify|test|check)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        $PYTHON manage_cli.py verify
        ;;

    web)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        echo -e "${GREEN}Starting Bybit V5 Web Executive Dashboard on http://127.0.0.1:8080...${RESET}"
        $PYTHON web_server.py
        ;;

    logs)
        echo -e "${CYAN}Tailing live application logs... (Ctrl+C to exit)${RESET}"
        if [ -f "logs/engine.log" ]; then
            tail -n 50 -f logs/engine.log
        else
            $SYSTEMCTL_CMD logs -u bybit-engine -f || journalctl --user -u bybit-engine -f
        fi
        ;;

    panic)
        echo -e "${RED}${BOLD}====================================================${RESET}"
        echo -e "${RED}${BOLD}       EMERGENCY PANIC LIQUIDATION TRIGGERED        ${RESET}"
        echo -e "${RED}${BOLD}====================================================${RESET}"
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found.${RESET}"
            exit 1
        fi
        $PYTHON manage_cli.py panic
        ;;

    update)
        echo -e "${YELLOW}Fetching latest repository updates...${RESET}"
        git pull || true
        echo -e "${YELLOW}Syncing virtualenv packages...${RESET}"
        $PYTHON -m pip install -r requirements.txt --quiet
        echo -e "${YELLOW}Restarting trading engine service...${RESET}"
        $SYSTEMCTL_CMD restart bybit-engine || echo "Service restarted."
        echo -e "${GREEN}✓ Update and restart completed successfully.${RESET}"
        ;;

    start)
        echo -e "${GREEN}Starting bybit-engine service...${RESET}"
        $SYSTEMCTL_CMD start bybit-engine
        echo -e "${GREEN}✓ Started.${RESET}"
        ;;

    stop)
        echo -e "${YELLOW}Stopping bybit-engine service...${RESET}"
        $SYSTEMCTL_CMD stop bybit-engine
        echo -e "${YELLOW}✓ Stopped.${RESET}"
        ;;

    restart)
        echo -e "${YELLOW}Restarting bybit-engine service...${RESET}"
        $SYSTEMCTL_CMD restart bybit-engine
        echo -e "${GREEN}✓ Restarted.${RESET}"
        ;;

    help|--help|-h)
        usage
        ;;

    *)
        echo -e "${RED}Unknown command: $COMMAND${RESET}\n"
        usage
        exit 1
        ;;
esac
