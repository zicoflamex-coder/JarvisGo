#!/bin/bash
# Jarvis bot installer for a fresh Ubuntu/Debian VPS. Run as root: bash jarvis_setup.sh
set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
RAW="https://raw.githubusercontent.com/zicoflamex-coder/JarvisGo/main"
BOT_FILE="jarvis_v21.py"

if [ "$(id -u)" != "0" ]; then
  echo "Run this as root."
  exit 1
fi

echo "== 1/7 Network test =="
TG=$(curl -s -o /dev/null -m 15 -w "%{http_code}" https://api.telegram.org || true)
GQ=$(curl -s -o /dev/null -m 15 -w "%{http_code}" https://api.groq.com/openai/v1/models || true)
GH=$(curl -s -o /dev/null -m 15 -w "%{http_code}" "$RAW/$BOT_FILE" || true)
echo "Telegram: $TG   (000 = blocked)"
echo "Groq:     $GQ   (401 = good, 403 or 000 = blocked)"
echo "GitHub:   $GH   (200 = good, 404 = file name or repo wrong)"
if [ "$TG" = "000" ]; then
  echo "Telegram is NOT reachable from this server. Stop here and ask for another location."
  exit 1
fi
if [ "$GQ" = "000" ] || [ "$GQ" = "403" ]; then
  echo "WARNING: Groq looks blocked from this server. The bot will work but AI features will not."
fi

echo "== 2/7 Installing packages =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl

echo "== 3/7 Swap (1 GB) =="
if ! swapon --show | grep -q .; then
  (fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab) || echo "Swap not supported here, skipping."
fi

echo "== 4/7 Downloading the bot =="
id jarvis >/dev/null 2>&1 || useradd -r -m -d /opt/jarvis -s /usr/sbin/nologin jarvis
mkdir -p /opt/jarvis
DOWNLOAD_TMP=$(mktemp /opt/jarvis/bot.py.XXXXXX)
trap 'rm -f "$DOWNLOAD_TMP"' EXIT
if ! curl -fsSL --retry 2 --connect-timeout 15 -o "$DOWNLOAD_TMP" "$RAW/$BOT_FILE"; then
  if [ -f /root/bot.py ]; then
    cp /root/bot.py "$DOWNLOAD_TMP"
  else
    echo "ERROR: could not download $BOT_FILE from GitHub. Upload it to the repo first."
    exit 1
  fi
fi
SIZE=$(wc -c < "$DOWNLOAD_TMP")
if [ "$SIZE" -lt 100000 ]; then
  echo "ERROR: bot.py looks wrong (size $SIZE bytes, expected about 270000)."
  exit 1
fi
if ! python3 -m py_compile "$DOWNLOAD_TMP"; then
  echo "ERROR: downloaded bot.py failed Python syntax validation; existing installation was not replaced."
  exit 1
fi
install -m 0644 "$DOWNLOAD_TMP" /opt/jarvis/bot.py
rm -f "$DOWNLOAD_TMP"
echo "bot.py size: $SIZE bytes (syntax validated)"

echo "== 5/7 Python packages =="
cat > /opt/jarvis/requirements.txt << 'REQEOF'
aiogram>=3.7,<4
aiosqlite>=0.20
aiohttp>=3.9
python-dotenv>=1.0
REQEOF
python3 -m venv /opt/jarvis/venv
/opt/jarvis/venv/bin/pip install --upgrade pip -q
/opt/jarvis/venv/bin/pip install -r /opt/jarvis/requirements.txt -q
/opt/jarvis/venv/bin/pip install -q edge-tts || echo "edge-tts not installed (voice replies will be off)."

echo "== 6/7 Settings =="
read -r -p "BOT_TOKEN (from BotFather): " BOT_TOKEN
read -r -p "AI_API_KEY (gsk_...): " AI_API_KEY
read -r -p "AI_API_KEY_2 (second key, optional - press Enter to skip): " AI_API_KEY_2
read -r -p "OWNER_ID (your numeric Telegram id): " OWNER_ID
BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d ' \r\t')
AI_API_KEY=$(echo "$AI_API_KEY" | tr -d ' \r\t')
AI_API_KEY_2=$(echo "$AI_API_KEY_2" | tr -d ' \r\t')
OWNER_ID=$(echo "$OWNER_ID" | tr -d ' \r\t')
if [ -z "$BOT_TOKEN" ]; then
  echo "BOT_TOKEN is empty. Run the script again."
  exit 1
fi
cat > /opt/jarvis/.env << ENVEOF
BOT_TOKEN=$BOT_TOKEN
AI_API_KEY=$AI_API_KEY
OWNER_ID=$OWNER_ID
DB_PATH=/opt/jarvis/jarvis_bot.db
ENVEOF
if [ -n "$AI_API_KEY_2" ]; then
  echo "AI_API_KEY_2=$AI_API_KEY_2" >> /opt/jarvis/.env
fi
chmod 600 /opt/jarvis/.env
chown -R jarvis:jarvis /opt/jarvis

echo "== 7/7 Starting the service =="
cat > /etc/systemd/system/jarvis.service << 'UNITEOF'
[Unit]
Description=Jarvis Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=jarvis
WorkingDirectory=/opt/jarvis
EnvironmentFile=/opt/jarvis/.env
ExecStart=/opt/jarvis/venv/bin/python /opt/jarvis/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable jarvis >/dev/null 2>&1
systemctl restart jarvis
sleep 8
echo
echo "Status: $(systemctl is-active jarvis)"
journalctl -u jarvis -n 15 --no-pager
echo
echo "Done. Useful commands:"
echo "  journalctl -u jarvis -f      (live logs, Ctrl+C to exit)"
echo "  systemctl restart jarvis     (restart the bot)"
