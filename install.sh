#!/usr/bin/env bash
# Mini Studio — one-command install for Ubuntu 22.04 / 24.04
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/samsamcos/mini-studio/main/install.sh)
# Or:    git clone https://github.com/samsamcos/mini-studio && cd mini-studio && bash install.sh

set -euo pipefail
STUDIO_DIR="/opt/mini-studio"
PROJECTS_DIR="/opt/studio/projects"
MODELS_DIR="/opt/models"
WHISPER_DIR="/opt/studio/whisper_models"
VENV="/opt/studio-env"
LLAMA_BIN="/usr/local/bin/llama-server"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }
ask()   { echo -e "${YELLOW}[?]${NC} $*"; }

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║     Mini Studio — Node 4 Installer   ║"
echo "  ║  CPU-Only AI Video Editing Pipeline  ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

[ "$(id -u)" -eq 0 ] || error "Run as root: sudo bash install.sh"

# ── 1. System packages ──────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    ffmpeg python3.12 python3.12-venv python3.12-dev \
    git curl wget build-essential software-properties-common \
    docker.io docker-compose-plugin \
    sqlite3 unzip

systemctl enable --now docker

# ── 2. Clone repo ────────────────────────────────────────────────────────────
if [ -d "$STUDIO_DIR/.git" ]; then
    info "Updating existing repo at $STUDIO_DIR..."
    git -C "$STUDIO_DIR" pull --ff-only
else
    info "Cloning mini-studio to $STUDIO_DIR..."
    git clone https://github.com/samsamcos/mini-studio "$STUDIO_DIR"
fi

mkdir -p "$PROJECTS_DIR" "$MODELS_DIR" "$WHISPER_DIR" /opt/studio/incoming

# ── 3. Python environment ─────────────────────────────────────────────────────
info "Creating Python 3.12 virtual environment..."
python3.12 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q \
    flask flask-cors requests faster-whisper google-genai \
    numpy ffmpeg-python

info "Python environment ready at $VENV"

# ── 4. llama.cpp server ───────────────────────────────────────────────────────
if [ ! -f "$LLAMA_BIN" ]; then
    warn "llama-server not found at $LLAMA_BIN"
    warn "Download a prebuilt binary or build from source:"
    warn "  https://github.com/ggerganov/llama.cpp/releases"
    warn "  Place the binary at $LLAMA_BIN and chmod +x it"
    warn "Skipping — models will not load until llama-server is installed."
else
    info "llama-server found: $($LLAMA_BIN --version 2>&1 | head -1)"
fi

# ── 5. GGUF models ────────────────────────────────────────────────────────────
warn "LLM models must be downloaded manually (large files):"
warn "  Gemma 2 9B Q4_K_M  (~5.5 GB) → $MODELS_DIR/gemma-2-9b-it-q4_k_m.gguf"
warn "  Qwen 2.5 3B Q4_K_M (~2 GB)   → $MODELS_DIR/qwen2.5-3b-instruct-q4_k_m.gguf"
warn "  Download from: https://huggingface.co/bartowski"
warn "  Or use: huggingface-cli download bartowski/gemma-2-9b-it-GGUF"

# ── 6. Pocket TTS ─────────────────────────────────────────────────────────────
POCKET_ENV="/opt/pocket-env"
if [ ! -d "$POCKET_ENV" ]; then
    info "Setting up Pocket TTS environment..."
    python3.12 -m venv "$POCKET_ENV"
    # Install from local path if setup script exists, otherwise warn
    if [ -f "$STUDIO_DIR/setup_pocket_tts.sh" ]; then
        bash "$STUDIO_DIR/setup_pocket_tts.sh" "$POCKET_ENV"
    else
        warn "Pocket TTS requires manual install — see setup_pocket_tts.sh"
    fi
else
    info "Pocket TTS environment exists at $POCKET_ENV"
fi

# ── 7. Systemd services ───────────────────────────────────────────────────────
info "Installing systemd service units..."
SERVICES_SRC="$STUDIO_DIR/systemd"
if [ -d "$SERVICES_SRC" ]; then
    cp "$SERVICES_SRC"/*.service /etc/systemd/system/ 2>/dev/null || warn "No .service files found in $SERVICES_SRC"
    systemctl daemon-reload
    info "Service units installed."
else
    warn "No systemd/ directory found in repo. Services not installed."
fi

# ── 8. .env setup ─────────────────────────────────────────────────────────────
ENV_FILE="$STUDIO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    info "Creating .env from template..."
    cp "$STUDIO_DIR/.env.example" "$ENV_FILE"

    echo ""
    echo "  ┌─────────────────────────────────────────┐"
    echo "  │        API Key Configuration            │"
    echo "  └─────────────────────────────────────────┘"
    echo ""

    ask "Enter your Gemini API key (or press Enter to skip):"
    read -r GEMINI_KEY
    if [ -n "$GEMINI_KEY" ]; then
        sed -i "s/GEMINI_API_KEY=.*/GEMINI_API_KEY=$GEMINI_KEY/" "$ENV_FILE"
        info "Gemini key saved."
    else
        warn "Gemini key skipped — Gemini vision tagging will be unavailable."
    fi

    ask "Enter your Groq API key (or press Enter to skip):"
    read -r GROQ_KEY
    if [ -n "$GROQ_KEY" ]; then
        sed -i "s/GROQ_API_KEY=.*/GROQ_API_KEY=$GROQ_KEY/" "$ENV_FILE"
        info "Groq key saved."
    else
        warn "Groq key skipped — STT fallback will be unavailable."
    fi

    chmod 600 "$ENV_FILE"
    info ".env created at $ENV_FILE (mode 600, not tracked by git)"
else
    info ".env already exists — skipping key prompts."
fi

# ── 9. Enable services ────────────────────────────────────────────────────────
info "Enabling and starting pipeline services..."
ENABLE_SERVICES="stt-consensus voiceover gemini-ingest highlight autoedit studio-settings"
DEMAND_SERVICES="llama-qwen llama-gemma"

for svc in $ENABLE_SERVICES; do
    if systemctl list-unit-files "${svc}.service" &>/dev/null; then
        systemctl enable --now "$svc" 2>/dev/null && info "Started: $svc" || warn "Could not start: $svc (may need llama-server first)"
    fi
done

for svc in $DEMAND_SERVICES; do
    if systemctl list-unit-files "${svc}.service" &>/dev/null; then
        systemctl disable "$svc" 2>/dev/null
        info "Disabled (on-demand): $svc"
    fi
done

# Pocket TTS
if systemctl list-unit-files "pocket-tts.service" &>/dev/null; then
    systemctl enable --now pocket-tts 2>/dev/null && info "Started: pocket-tts" || warn "pocket-tts failed — check $POCKET_ENV"
fi

# ── 10. OpenCut (Docker) ──────────────────────────────────────────────────────
COMPOSE_FILE="/opt/studio/docker-compose.slim.yml"
if [ -f "$COMPOSE_FILE" ]; then
    info "Starting OpenCut editor (Docker)..."
    docker compose -f "$COMPOSE_FILE" up -d --no-deps web db redis serverless-redis-http \
        && info "OpenCut running at http://$(hostname -I | awk '{print $1}'):9500" \
        || warn "Docker compose failed — check logs: docker compose -f $COMPOSE_FILE logs web"
else
    warn "docker-compose.slim.yml not found at $COMPOSE_FILE — OpenCut not started."
fi

# ── 11. Quick health check ────────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │          Health Check                   │"
echo "  └─────────────────────────────────────────┘"

IP=$(hostname -I | awk '{print $1}')
for port_name in "85:Dashboard" "9540:Settings" "9544:Highlights" "9545:Auto-Edit" "5020:Pocket TTS" "9500:OpenCut"; do
    port="${port_name%%:*}"
    name="${port_name##*:}"
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null || echo "err")
    if [ "$code" = "200" ] || [ "$code" = "404" ]; then
        echo -e "  ${GREEN}✓${NC}  $name — http://$IP:$port"
    else
        echo -e "  ${YELLOW}?${NC}  $name — http://$IP:$port (code: $code)"
    fi
done

echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │       Install Complete                  │"
echo "  └─────────────────────────────────────────┘"
echo ""
echo "  Open: http://$IP:85"
echo "  Auto-Edit: http://$IP:9545"
echo "  OpenCut: http://$IP:9500"
echo ""
warn "Models still needed (download manually to $MODELS_DIR):"
warn "  gemma-2-9b-it-q4_k_m.gguf (~5.5 GB)"
warn "  qwen2.5-3b-instruct-q4_k_m.gguf (~2 GB)"
echo ""
info "Done. Add your models, then run a job at http://$IP:9545"
