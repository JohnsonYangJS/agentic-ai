#!/usr/bin/env bash
# serve_dashboard_8510.sh — 启动/重启 morning-newspaper 早报静态页服务
#
# 服务已由 systemd 托管：morning-newspaper-dashboard.service
# 本脚本改为委托 systemd，避免两套管理方式互相打架：
#   旧实现用 pkill + nohup 起进程，pkill 会误杀 systemd 托管的实例，
#   而 Restart=always 又会把它拉起，导致端口反复抢占。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
UNIT="morning-newspaper-dashboard.service"

if [ ! -d "$RUNTIME_DIR" ]; then
    echo "runtime dir not found: $RUNTIME_DIR" >&2
    exit 1
fi

if systemctl list-unit-files "${UNIT}" >/dev/null 2>&1 \
   && systemctl cat "${UNIT}" >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable "${UNIT}" >/dev/null 2>&1 || true
    systemctl restart "${UNIT}"
    sleep 1
    if systemctl is-active --quiet "${UNIT}"; then
        PID=$(systemctl show -p MainPID --value "${UNIT}")
        echo "started 8510 server (systemd) pid=${PID} serving ${RUNTIME_DIR}"
        echo "  status: systemctl status ${UNIT}"
        echo "  logs  : journalctl -u ${UNIT} -f"
    else
        echo "ERROR: ${UNIT} failed to start" >&2
        systemctl status "${UNIT}" --no-pager >&2 || true
        exit 1
    fi
    exit 0
fi

# 回退：没有 unit 时用 nohup 直接起（老行为）
echo "WARNING: ${UNIT} not installed, falling back to nohup" >&2
cd "$RUNTIME_DIR"
nohup python3 -m http.server 8510 --bind 0.0.0.0 >/tmp/morning-newspaper-8510.log 2>&1 &
echo $! > /tmp/morning-newspaper-8510.pid
echo "started 8510 server pid=$(cat /tmp/morning-newspaper-8510.pid) serving ${RUNTIME_DIR}"
