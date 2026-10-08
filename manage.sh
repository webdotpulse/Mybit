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
    echo "  mainnet  - Switch environment to Live Mainnet (api.bybit.com)"
    echo "  testnet  - Switch environment to Testnet (api-testnet.bybit.com)"
    echo "  web         - Launch Web Dashboard in foreground (e.g. ./manage.sh web --public)"
    echo "  web-start   - Start 24/7 background Web Dashboard systemd daemon"
    echo "  web-stop    - Stop 24/7 background Web Dashboard systemd daemon"
    echo "  web-restart - Restart 24/7 background Web Dashboard systemd daemon"
    echo "  web-status  - View status of 24/7 background Web Dashboard service"
    echo "  backtest    - Run quantitative backtest simulation on historical Bybit data"
    echo "  logs        - Stream live real-time engine execution logs"
    echo "  panic       - EMERGENCY: Cancel all open orders and market close all positions"
    echo "  update      - Pull latest git updates, sync virtualenv, and restart daemon"
    echo "  start       - Start background trading engine daemon"
    echo "  stop        - Stop background trading engine daemon"
    echo "  restart     - Cleanly restart background trading engine daemon"
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

    mainnet|live|prod)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        $PYTHON manage_cli.py mainnet
        ;;

    testnet|demo)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        $PYTHON manage_cli.py testnet
        ;;

    web)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        shift
        HAS_PUBLIC=false
        for arg in "$@"; do
            if [ "$arg" == "--public" ] || [ "$arg" == "-p" ]; then
                HAS_PUBLIC=true
                break
            fi
        done

        if [ "$HAS_PUBLIC" = true ]; then
            echo -e "${GREEN}Starting Bybit V5 Web Executive Dashboard on all interfaces (http://0.0.0.0:8080) [VIEW-ONLY]...${RESET}"
            echo -e "${CYAN}Access directly in your browser: ${BOLD}http://<YOUR_IP>:8080${RESET}\n"
            $PYTHON web_server.py "$@"
        else
            echo -e "${GREEN}Starting Bybit V5 Web Executive Dashboard on http://127.0.0.1:8080...${RESET}"
            echo -e "${CYAN}If running on a remote server, access via SSH tunnel from your local computer:${RESET}"
            echo -e "  ${YELLOW}ssh -L 8080:localhost:8080 $(whoami)@<SERVER_IP>${RESET}"
            echo -e "  Then open in your browser: ${BOLD}http://localhost:8080${RESET}"
            echo -e "${CYAN}Or start publicly with:${RESET} ${YELLOW}./manage.sh web --public${RESET}\n"
            $PYTHON web_server.py "$@"
        fi
        ;;

    web-start)
        echo -e "${GREEN}Starting 24/7 bybit-web background service...${RESET}"
        systemctl --user start bybit-web
        echo -e "${GREEN}✓ Started.${RESET}"
        ;;

    web-stop)
        echo -e "${YELLOW}Stopping 24/7 bybit-web background service...${RESET}"
        systemctl --user stop bybit-web
        echo -e "${YELLOW}✓ Stopped.${RESET}"
        ;;

    web-restart)
        echo -e "${YELLOW}Restarting 24/7 bybit-web background service...${RESET}"
        systemctl --user restart bybit-web
        echo -e "${GREEN}✓ Restarted.${RESET}"
        ;;

    web-status)
        systemctl --user status bybit-web --no-pager || true
        ;;

    backtest|sim|simulate)
        if [ ! -f "$PYTHON" ]; then
            echo -e "${RED}Error: Virtual environment not found. Run ./install.sh first.${RESET}"
            exit 1
        fi
        shift
        $PYTHON manage_cli.py backtest "$@"
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
