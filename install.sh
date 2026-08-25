#!/bin/bash
# CT103 Mini Studio — One-Command Installer
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/samsamcos/mini-studio/main/install.sh)
#
# Target: Debian 12 LXC on Proxmox (CT103, IP set by DHCP)
# Installs: Edit Director · OmniRoute · dashboard · auto pipeline · file watcher · nodeagent

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
die()     { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║       CT103 Mini Studio — Auto Installer             ║"
echo "║  Edit Director · OmniRoute · TTS · STT · Watcher    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

[[ $EUID -ne 0 ]] && die "Run as root"

HOST_IP=$(hostname -I | awk '{print $1}')
REPO_RAW="https://raw.githubusercontent.com/samsamcos/mini-studio/main"
STUDIO_DIR="/opt/studio"
VENV="$STUDIO_DIR/venv"

# ── Prompt config ──────────────────────────────────────────────────────────
echo "── API Keys ──────────────────────────────────────────────"
read -rp "  Groq API key (for OmniRoute + rush LLM fallback): " GROQ_KEY
read -rp "  Telegram bot token: " TG_TOKEN
read -rp "  Telegram chat ID [7819702619]: " TG_CHAT
TG_CHAT=${TG_CHAT:-7819702619}
read -rp "  Pocket TTS URL [http://192.168.0.235:5020]: " POCKET_TTS
POCKET_TTS=${POCKET_TTS:-http://192.168.0.235:5020}
read -rp "  OpenCut URL [http://${HOST_IP}:9500]: " OPENCUT_URL
OPENCUT_URL=${OPENCUT_URL:-http://${HOST_IP}:9500}
echo ""

# ── Step 1: System packages ────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ffmpeg git curl wget docker.io
systemctl enable docker --now
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
mkdir -p "$STUDIO_DIR/dashboard" "$STUDIO_DIR/edit_director" \
         "$STUDIO_DIR/pipeline" "$STUDIO_DIR/tts_output" \
         "$STUDIO_DIR/inbox" "$STUDIO_DIR/projects" \
         "$STUDIO_DIR/logs" "$STUDIO_DIR/voices" \
         /root/nodeagent

for f in auto.py watcher.py auto.html index.html archive_browser.py ai-bridge.html; do
    wget -q -O "$STUDIO_DIR/dashboard/$f" "$REPO_RAW/dashboard/$f" && echo "  dashboard/$f"
done

wget -q -O "$STUDIO_DIR/edit_director/director.py" \
    "$REPO_RAW/edit_director/director.py" && echo "  edit_director/director.py"

wget -q -O "$STUDIO_DIR/pipeline/tts_stt_pipeline.py" \
    "$REPO_RAW/pipeline/tts_stt_pipeline.py" && echo "  pipeline/tts_stt_pipeline.py"

wget -q -O "$STUDIO_DIR/channel_export.py" \
    "$REPO_RAW/channel_export.py" && echo "  channel_export.py"

wget -q -O "$STUDIO_DIR/vlog_service.py" \
    "$REPO_RAW/vlog_service.py" && echo "  vlog_service.py"

wget -q -O "$STUDIO_DIR/shorts_service.py" \
    "$REPO_RAW/shorts_service.py" && echo "  shorts_service.py"

success "Files downloaded"

# ── Step 4: .env ───────────────────────────────────────────────────────────
if [ ! -f "$STUDIO_DIR/.env" ]; then
cat > "$STUDIO_DIR/.env" << ENVEOF
GROQ_API_KEY=${GROQ_KEY}

TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}

OMNIROUTE_URL=http://127.0.0.1:20128
POLLINATIONS_URL=https://text.pollinations.ai

WHISPER_URL=http://127.0.0.1:8421
POCKET_TTS_URL=${POCKET_TTS}
VOICE_WAV_PATH=/opt/studio/voices/voice_clone.wav
TTS_URL=http://127.0.0.1:8422

OPENCUT_URL=${OPENCUT_URL}
DIRECTOR_HOST=${HOST_IP}
DIRECTOR_PORT=9533
PROJECTS_DIR=/opt/studio/projects
WATCH_DIR=/opt/studio/inbox
ENVEOF
    success ".env written"
else
    success ".env already exists — not overwritten"
fi

# ── Step 5: OmniRoute (local LLM router, no external dependency) ───────────
info "Starting OmniRoute on 127.0.0.1:20128..."
if docker ps -a --filter name=omniroute --format '{{.Names}}' | grep -q omniroute; then
    docker start omniroute 2>/dev/null || true
else
    docker run -d \
        --name omniroute \
        --restart unless-stopped \
        -p 127.0.0.1:20128:20128 \
        -e GROQ_API_KEY="${GROQ_KEY}" \
        -e PORT=20128 \
        -e HOSTNAME=0.0.0.0 \
        diegosouzapw/omniroute:latest
fi
sleep 4
if curl -s --max-time 5 http://127.0.0.1:20128/v1/models | grep -q '"object":"list"'; then
    success "OmniRoute running (127.0.0.1:20128)"
else
    warn "OmniRoute didn't respond — edit director will fall back to Pollinations/Groq"
fi

# ── Step 6: nodeagent stub ─────────────────────────────────────────────────
if [ ! -f /root/nodeagent/nodeagent.py ]; then
    cat > /root/nodeagent/nodeagent.py << 'PYEOF'
from flask import Flask, jsonify
app = Flask(__name__)
@app.route("/agent/health")
def health(): return jsonify({"status":"ok","node":"ct103-mini-studio"})
if __name__ == "__main__": app.run(host="0.0.0.0",port=7070)
PYEOF
fi

# ── Step 7: systemd services ───────────────────────────────────────────────
info "Installing services..."
for svc in edit-director studio-auto studio-watcher studio-dashboard \
           channel-export tts-stt-pipeline nodeagent \
           vlog-builder shorts-builder; do
    wget -q -O "/etc/systemd/system/${svc}.service" \
        "$REPO_RAW/services/${svc}.service" && echo "  $svc" || warn "  missing: $svc"
done

systemctl daemon-reload
for svc in edit-director studio-auto studio-watcher studio-dashboard \
           tts-stt-pipeline nodeagent vlog-builder shorts-builder; do
    systemctl enable "$svc" 2>/dev/null
    systemctl restart "$svc" && echo "  started: $svc" || warn "  $svc failed to start"
done

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║           Mini Studio Install Complete!                  ║"
echo "╠══════════════════════════════════════════════════════════╣"
printf "║  Dashboard:      http://%-32s║\n" "${HOST_IP}:85"
printf "║  Edit Director:  http://%-32s║\n" "${HOST_IP}:9533"
printf "║  Demo Page:      http://%-32s║\n" "${HOST_IP}:9533/demo"
printf "║  Vlog Builder:   http://%-32s║\n" "${HOST_IP}:9534"
printf "║  Shorts Builder: http://%-32s║\n" "${HOST_IP}:9535"
printf "║  Trip Context:   http://%-32s║\n" "${HOST_IP}:9533/context"
printf "║  OmniRoute:      http://%-32s║\n" "127.0.0.1:20128 (local only)"
printf "║  Auto API:       http://%-32s║\n" "${HOST_IP}:9530"
printf "║  TTS/STT API:    http://%-32s║\n" "${HOST_IP}:9532/api/status"
printf "║  NodeAgent:      http://%-32s║\n" "${HOST_IP}:7070/agent/health"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  NEXT: Drop your voice clone WAV to:                    ║"
echo "║    /opt/studio/voices/voice_clone.wav                   ║"
echo "║  NEXT: Edit /opt/studio/.env to set all API keys        ║"
echo "║  NEXT: Copy OpenCut's ai-bridge.html to its web root    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Logs: tail -f /opt/studio/logs/*.log"
echo ""
