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

get_web_systemctl() {
    if [ -f "/etc/systemd/system/bybit-web.service" ]; then
        if [ "$EUID" -eq 0 ]; then
            echo "systemctl"
        elif sudo -n true 2>/dev/null; then
            echo "sudo systemctl"
        else
            echo "systemctl"
        fi
    else
        echo "systemctl --user"
    fi
}

install_web_service() {
    echo -e "${YELLOW}Provisioning bybit-web.service daemon on this machine...${RESET}"
    IS_ROOT=false
    CAN_SUDO=false
    if [ "$EUID" -eq 0 ]; then
        IS_ROOT=true
    elif sudo -n true 2>/dev/null; then
        CAN_SUDO=true
    fi

    CURRENT_USER="$(whoami)"

    if [ "$IS_ROOT" = true ] || [ "$CAN_SUDO" = true ]; then
        SERVICE_FILE="/tmp/bybit-web.service"
        cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Bybit V5 Autonomous Engine - Live Executive Web Dashboard (View-Only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/$PYTHON $SCRIPT_DIR/web_server.py --port 8088 --public --read-only
Restart=always
RestartSec=3s
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOF
        if [ "$IS_ROOT" = true ]; then
            cp "$SERVICE_FILE" /etc/systemd/system/bybit-web.service
            systemctl daemon-reload
            systemctl enable --now bybit-web.service
        else
            sudo cp "$SERVICE_FILE" /etc/systemd/system/bybit-web.service
            sudo systemctl daemon-reload
            sudo systemctl enable --now bybit-web.service
        fi
        rm -f "$SERVICE_FILE"
        echo -e "${GREEN}✓ Installed and started system service at /etc/systemd/system/bybit-web.service${RESET}"
    else
        USER_SERVICE_DIR="$HOME/.config/systemd/user"
        mkdir -p "$USER_SERVICE_DIR"
        loginctl enable-linger "$CURRENT_USER" 2>/dev/null || true
        cat <<EOF > "$USER_SERVICE_DIR/bybit-web.service"
[Unit]
Description=Bybit V5 Autonomous Engine - Live Executive Web Dashboard (View-Only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/$PYTHON $SCRIPT_DIR/web_server.py --port 8088 --public --read-only
Restart=always
RestartSec=3s
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now bybit-web.service
        echo -e "${GREEN}✓ Installed and started user service at $USER_SERVICE_DIR/bybit-web.service${RESET}"
    fi
}

ensure_web_service_installed() {
    if [ ! -f "/etc/systemd/system/bybit-web.service" ] && [ ! -f "$HOME/.config/systemd/user/bybit-web.service" ]; then
        install_web_service
    fi
}

usage() {
    echo -e "${CYAN}${BOLD}Bybit V5 Autonomous Engine - Control Utility${RESET}"
    echo "Usage: ./manage.sh [command]"
    echo ""
    echo "Available Commands:"
    echo "  status      - Display live PnL, active positions, win rate, and circuit breakers"
    echo "  verify      - Deep diagnostic test of Bybit API keys, IP whitelist & connectivity"
    echo "  mainnet     - Switch environment to Live Mainnet (api.bybit.com)"
    echo "  testnet     - Switch environment to Testnet (api-testnet.bybit.com)"
    echo "  web         - Launch Web Dashboard in foreground (e.g. ./manage.sh web --public)"
    echo "  web-start   - Start 24/7 background Web Dashboard systemd daemon"
    echo "  web-stop    - Stop 24/7 background Web Dashboard systemd daemon"
    echo "  web-restart - Restart 24/7 background Web Dashboard systemd daemon"
    echo "  web-status  - View status of 24/7 background Web Dashboard service"
    echo "  web-install - Install/reinstall 24/7 background Web Dashboard service"
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
        ensure_web_service_installed
        W_CMD="$(get_web_systemctl)"
        echo -e "${GREEN}Starting 24/7 bybit-web background service...${RESET}"
        $W_CMD start bybit-web
        echo -e "${GREEN}✓ Started.${RESET}"
        ;;

    web-stop)
        W_CMD="$(get_web_systemctl)"
        echo -e "${YELLOW}Stopping 24/7 bybit-web background service...${RESET}"
        $W_CMD stop bybit-web || true
        echo -e "${YELLOW}✓ Stopped.${RESET}"
        ;;

    web-restart)
        ensure_web_service_installed
        W_CMD="$(get_web_systemctl)"
        echo -e "${YELLOW}Restarting 24/7 bybit-web background service...${RESET}"
        $W_CMD restart bybit-web
        echo -e "${GREEN}✓ Restarted.${RESET}"
        ;;

    web-status)
        ensure_web_service_installed
        W_CMD="$(get_web_systemctl)"
        $W_CMD status bybit-web --no-pager || true
        ;;

    web-install)
        install_web_service
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
