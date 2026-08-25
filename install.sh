#!/bin/bash
# CT103 Mini Studio — One-Command Installer
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/samsamcos/mini-studio/main/install.sh)
#
# Target: Debian 12 LXC on Proxmox (192.168.0.10, CT103)
# Installs: studio dashboard, auto pipeline, file watcher, TTS/STT pipeline, nodeagent
# NOTE: Pocket TTS and Fish TTS have large model downloads — run on a fast connection

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
die()     { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║         CT103 Mini Studio — Auto Installer           ║"
echo "║  Auto Pipeline · TTS · STT · Watcher · NodeAgent    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

[[ $EUID -ne 0 ]] && die "Run as root"

HOST_IP=$(hostname -I | awk '{print $1}')
REPO_RAW="https://raw.githubusercontent.com/samsamcos/mini-studio/main"
STUDIO_DIR="/opt/studio"
VENV="$STUDIO_DIR/venv"

# ── Prompt config ──────────────────────────────────────────────────────────
echo "── API Keys ──────────────────────────────────────────────"
read -rp "  Groq API key: " GROQ_KEY
read -rp "  Telegram bot token: " TG_TOKEN
read -rp "  Telegram chat ID [7819702619]: " TG_CHAT
TG_CHAT=${TG_CHAT:-7819702619}
echo ""

# ── Step 1: System packages ────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ffmpeg git curl wget
success "System packages installed"

# ── Step 2: Python venv ────────────────────────────────────────────────────
info "Creating Python venv at $VENV..."
mkdir -p "$STUDIO_DIR"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q \
    flask requests faster-whisper numpy \
    groq watchdog python-dotenv
success "Python packages installed"

# ── Step 3: Pull code ──────────────────────────────────────────────────────
info "Downloading mini-studio files..."
mkdir -p "$STUDIO_DIR/dashboard" "$STUDIO_DIR/pipeline" \
         "$STUDIO_DIR/tts_output" "$STUDIO_DIR/inbox" \
         "$STUDIO_DIR/logs" /root/nodeagent

for f in auto.py watcher.py auto.html index.html archive_browser.py; do
    wget -q -O "$STUDIO_DIR/dashboard/$f" "$REPO_RAW/dashboard/$f" && echo "  dashboard/$f"
done

wget -q -O "$STUDIO_DIR/pipeline/tts_stt_pipeline.py" "$REPO_RAW/pipeline/tts_stt_pipeline.py" \
    && echo "  pipeline/tts_stt_pipeline.py"
wget -q -O "$STUDIO_DIR/channel_export.py" "$REPO_RAW/channel_export.py" \
    && echo "  channel_export.py"
success "Files downloaded"

# ── Step 4: .env ───────────────────────────────────────────────────────────
if [ ! -f "$STUDIO_DIR/.env" ]; then
cat > "$STUDIO_DIR/.env" << ENVEOF
GROQ_API_KEY=${GROQ_KEY}

TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}

POCKET_TTS_URL=http://127.0.0.1:5020
FISH_TTS_URL=http://127.0.0.1:5022
WHISPER_MODEL=small
TTS_OUT_DIR=/opt/studio/tts_output

WATCH_DIR=/opt/studio/inbox
ENVEOF
    success ".env written"
else
    success ".env already exists — not overwritten"
fi

# ── Step 5: nodeagent stub ─────────────────────────────────────────────────
if [ ! -f /root/nodeagent/nodeagent.py ]; then
    cat > /root/nodeagent/nodeagent.py << 'PYEOF'
from flask import Flask, jsonify
app = Flask(__name__)
@app.route("/agent/health")
def health(): return jsonify({"status":"ok","node":"ct103-mini-studio"})
if __name__ == "__main__": app.run(host="0.0.0.0",port=7070)
PYEOF
fi

# ── Step 6: systemd services ───────────────────────────────────────────────
info "Installing services..."
for svc in studio-auto studio-watcher studio-dashboard channel-export tts-stt-pipeline nodeagent; do
    wget -q -O "/etc/systemd/system/${svc}.service" "$REPO_RAW/services/${svc}.service" \
        && echo "  $svc" || warn "  missing: $svc"
done

systemctl daemon-reload
for svc in studio-auto studio-watcher studio-dashboard tts-stt-pipeline nodeagent; do
    systemctl enable "$svc" 2>/dev/null
    systemctl restart "$svc" && echo "  started: $svc" || warn "  $svc failed"
done

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║           Mini Studio Install Complete!              ║"
echo "╠══════════════════════════════════════════════════════╣"
printf "║  Dashboard:   http://%-32s║\n" "${HOST_IP}:85"
printf "║  Auto API:    http://%-32s║\n" "${HOST_IP}:9530"
printf "║  TTS/STT API: http://%-32s║\n" "${HOST_IP}:9532/api/status"
printf "║  NodeAgent:   http://%-32s║\n" "${HOST_IP}:7070/agent/health"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  NOTE: Pocket TTS (:5020) and Fish TTS (:5022)      ║"
echo "║  require separate model downloads — see README.md   ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Edit /opt/studio/.env to update API keys"
echo "Logs: tail -f /opt/studio/logs/*.log"
echo ""
