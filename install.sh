#!/usr/bin/env bash
# نصب روی سرور (Ubuntu/Debian): داخل پوشه‌ی پروژه اجرا کن:  sudo bash install.sh
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
SERVICE=shopbot

echo ">> نصب پیش‌نیازها"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

echo ">> ساخت venv و نصب پکیج‌ها"
sudo -u "$RUN_USER" python3 -m venv "$DIR/venv"
sudo -u "$RUN_USER" "$DIR/venv/bin/pip" install --upgrade pip
sudo -u "$RUN_USER" "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  sudo -u "$RUN_USER" cp "$DIR/.env.example" "$DIR/.env"
  echo ">> فایل .env ساخته شد. مقادیر واقعی را وارد کن:  nano $DIR/.env"
fi

echo ">> ساخت سرویس systemd"
cat > /etc/systemd/system/$SERVICE.service << UNIT
[Unit]
Description=Telegram Shop Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable $SERVICE

echo
echo "✅ نصب تمام شد."
echo "1) فایل .env را پر کن:      nano $DIR/.env"
echo "2) ربات را اجرا کن:         sudo systemctl start $SERVICE"
echo "3) دیدن لاگ:               sudo journalctl -u $SERVICE -f"
