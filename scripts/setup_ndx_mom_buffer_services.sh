#!/bin/bash

# Sets up systemd --user timers for NDX Momentum Buffered on the same VM as
# the other strategies (same mechanism as setup_factor_services.sh).
#
#   stockfilter-ndx-mom-buffer-rebalance: Mon-Fri 09:45 America/New_York.
#       The job exits immediately unless today is the first trading day of
#       the month (Alpaca market calendar), and is idempotent within a month.
#   stockfilter-ndx-mom-buffer-snapshot:  Mon-Fri 16:30 America/New_York.
#       Records equity + SPY/QQQ closes on trading days, and alerts if this
#       month's rebalance is missing.
#
# Times are pinned to America/New_York in the timer itself, so they track
# US daylight-saving changes even though the VM clock is UTC.
#
# Only run this AFTER reviewing a dry run:
#   ~/venv/bin/python -m scripts.run_ndx_mom_buffer rebalance --dry-run

set -e

echo "=== Setting up NDX Momentum Buffered systemd timers ==="
mkdir -p ~/.config/systemd/user

PROJECT_DIR=$(pwd)
VENV_PYTHON="$HOME/venv/bin/python"
UNIT_DIR="$HOME/.config/systemd/user"
LOG_FILE="$PROJECT_DIR/ndx_mom_buffer.log"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Virtual environment python not found at $VENV_PYTHON"
    exit 1
fi
if [ ! -f "$PROJECT_DIR/.env.ndx_mom_buffer" ]; then
    echo "ERROR: .env.ndx_mom_buffer not found in $PROJECT_DIR (copy .env.ndx_mom_buffer.example)."
    exit 1
fi

echo "Checking this strategy has its own paper account..."
"$VENV_PYTHON" -m scripts.run_ndx_mom_buffer check-accounts || {
    echo "ERROR: account check failed -- not installing timers."
    exit 1
}

create_unit_pair() {
    local NAME=$1      # rebalance | snapshot
    local COMMAND=$2   # CLI subcommand
    local WHEN=$3      # OnCalendar expression

    cat <<EOF > "$UNIT_DIR/stockfilter-ndx-mom-buffer-${NAME}.service"
[Unit]
Description=NDX Momentum Buffered ${NAME} (paper)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -u -m scripts.run_ndx_mom_buffer ${COMMAND}
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
EOF

    cat <<EOF > "$UNIT_DIR/stockfilter-ndx-mom-buffer-${NAME}.timer"
[Unit]
Description=NDX Momentum Buffered ${NAME} trigger

[Timer]
OnCalendar=${WHEN}
Persistent=true

[Install]
WantedBy=timers.target
EOF
    echo "Created stockfilter-ndx-mom-buffer-${NAME}.service/.timer (${WHEN})"
}

create_unit_pair "rebalance" "rebalance" "Mon..Fri *-*-* 09:45:00 America/New_York"
create_unit_pair "snapshot"  "snapshot"  "Mon..Fri *-*-* 16:30:00 America/New_York"

systemctl --user daemon-reload
systemctl --user enable --now stockfilter-ndx-mom-buffer-rebalance.timer
systemctl --user enable --now stockfilter-ndx-mom-buffer-snapshot.timer

echo ""
echo "=== Setup complete ==="
echo "Schedule:      systemctl --user list-timers 'stockfilter-ndx-*'"
echo "Log:           tail -f $LOG_FILE"
echo "Alerts:        cat $PROJECT_DIR/data/ndx_mom_buffer/ALERTS.log"
echo "Failed units:  systemctl --user --failed"
echo "Report:        $VENV_PYTHON -m scripts.run_ndx_mom_buffer report"
