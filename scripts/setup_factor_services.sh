#!/bin/bash

# Sets up systemd --user timers for the value/quality factor screeners on
# the same VM as the existing momentum scheduler (see setup_service.sh).
#
# Unlike scheduler.py (a continuously-running loop, since the momentum
# pipeline needs to fire daily), the factor screeners only need to run
# every ~80 days -- so instead of a loop, each gets a "oneshot" service
# fired by a daily systemd timer. The scripts themselves internally no-op
# on the days a rebalance isn't due, so firing daily is safe and cheap;
# the timer is just there to make sure they get *checked* every day.
#
# Prerequisites: .env.value and .env.quality must already exist in the
# project directory (copy from .env.value.example / .env.quality.example
# and fill in each strategy's own paper-account keys first).

set -e

echo "=== Setting up Value/Quality Factor Screener Systemd Timers ==="

mkdir -p ~/.config/systemd/user

PROJECT_DIR=$(pwd)
VENV_PYTHON="$HOME/venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Virtual environment python not found at $VENV_PYTHON"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env.value" ] || [ ! -f "$PROJECT_DIR/.env.quality" ]; then
    echo "ERROR: .env.value and/or .env.quality not found in $PROJECT_DIR."
    echo "Copy .env.value.example / .env.quality.example and fill in real keys first."
    exit 1
fi

echo "Project Directory: $PROJECT_DIR"
echo "Python Executable: $VENV_PYTHON"

create_unit_pair() {
    local NAME=$1        # "value" or "quality"
    local MODULE=$2      # e.g. "scripts.run_value_screen"
    local FIRE_TIME=$3   # e.g. "04:00:00" (system-local time)

    local SERVICE_FILE="$HOME/.config/systemd/user/stockfilter-${NAME}.service"
    local TIMER_FILE="$HOME/.config/systemd/user/stockfilter-${NAME}.timer"

    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=${NAME} factor screener (one-shot check; internally no-ops unless a rebalance is due)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -u -m $MODULE
StandardOutput=append:$PROJECT_DIR/${NAME}_screen.log
StandardError=append:$PROJECT_DIR/${NAME}_screen.log
EOF

    cat <<EOF > "$TIMER_FILE"
[Unit]
Description=Daily trigger for the ${NAME} factor screener

[Timer]
OnCalendar=*-*-* ${FIRE_TIME}
Persistent=true

[Install]
WantedBy=timers.target
EOF

    echo "Created $SERVICE_FILE and $TIMER_FILE"
}

# Staggered fire times so value and quality never run concurrently even on
# the rare day both happen to have a rebalance due -- a real rebalance run
# (full 1500-ticker scan) can take up to ~60 minutes and both currently
# share one Finnhub API key, so overlapping runs would double up against
# its rate limit. Also offset from scheduler.py's 16:15 Eastern momentum
# run. These are system-local times -- adjust to your VM's timezone/taste.
create_unit_pair "value" "scripts.run_value_screen" "04:00:00"
create_unit_pair "quality" "scripts.run_quality_screen" "07:00:00"

systemctl --user daemon-reload

systemctl --user enable --now stockfilter-value.timer
systemctl --user enable --now stockfilter-quality.timer

echo ""
echo "=== Setup Completed Successfully! ==="
echo "To check timer schedule:     systemctl --user list-timers"
echo "To view value screen logs:   tail -f $PROJECT_DIR/value_screen.log"
echo "To view quality screen logs: tail -f $PROJECT_DIR/quality_screen.log"
echo "To trigger one immediately for testing (bypasses the timer, still respects the 80-day rebalance guard):"
echo "  systemctl --user start stockfilter-value.service"
echo "  systemctl --user start stockfilter-quality.service"

# Enable linger so the timers keep firing after logout / across reboots,
# matching setup_service.sh's behavior for the momentum scheduler.
echo "Enabling linger for $USER..."
loginctl enable-linger
