#!/bin/bash

# Exit on error
set -e

echo "=== Setting up Stockfilter Systemd User Service ==="

# 1. Create systemd user config directory if missing
mkdir -p ~/.config/systemd/user

# 2. Get absolute paths
PROJECT_DIR=$(pwd)
VENV_PYTHON="$HOME/venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Virtual environment python not found at $VENV_PYTHON"
    exit 1
fi

echo "Project Directory: $PROJECT_DIR"
echo "Python Executable: $VENV_PYTHON"

# 3. Create the service file
SERVICE_FILE="$HOME/.config/systemd/user/stockfilter.service"

cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Top Analyst Momentum Screener Scheduler
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -u scheduler.py
Restart=always
RestartSec=10
StandardOutput=append:$PROJECT_DIR/scheduler.log
StandardError=append:$PROJECT_DIR/scheduler.log

[Install]
WantedBy=default.target
EOF

echo "Created systemd user service at: $SERVICE_FILE"

# 4. Reload systemd user daemon
systemctl --user daemon-reload

# 5. Enable and start the service
systemctl --user enable stockfilter.service
systemctl --user restart stockfilter.service

echo "Service enabled and started!"

# 6. Enable linger for the user so it runs on boot and persists after logout
echo "Enabling linger for $USER..."
loginctl enable-linger

echo "=== Setup Completed Successfully! ==="
echo "To check service status, run: systemctl --user status stockfilter.service"
echo "To view live logs, run: journalctl --user -u stockfilter.service -f"
