#!/bin/bash
# Pocket TTS local installer — run on any LXC/machine that needs local TTS
# Usage: bash setup_pocket_tts.sh [voice_wav_path] [port]
# Skips silently if already running.

set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[pocket-tts]${NC} $1"; }
success() { echo -e "${GREEN}[pocket-tts]${NC} $1"; }
warn()    { echo -e "${YELLOW}[pocket-tts]${NC} $1"; }

PORT=${2:-5020}
VENV=/opt/pocket-env
VOICE_WAV=${1:-/opt/studio/sync/voices/voice_clone.wav}
VOICES_DIR=/opt/studio/voices
SYNC_VOICES=/opt/studio/sync/voices

# Already running?
if curl -sf --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    success "Already running on :${PORT} — nothing to do"
    exit 0
fi

info "Installing Pocket TTS on this machine..."

# Python 3.12 preferred (Pocket TTS is faster on it), fall back to system python3
if command -v python3.12 &>/dev/null; then
    PY=python3.12
    apt-get install -y -qq python3.12-venv 2>/dev/null || true
else
    PY=python3
    apt-get install -y -qq python3-venv 2>/dev/null || true
fi

$PY -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q pocket-tts
success "pocket-tts pip package installed"

# ── Voice WAV sync setup ──────────────────────────────────────────────────────
# The canonical copy lives in SYNC_DIR (Syncthing keeps it in sync across machines).
# We symlink /opt/studio/voices/voice_clone.wav → sync/voices/voice_clone.wav
# so every app uses the same path regardless of machine.

mkdir -p "$SYNC_VOICES" "$VOICES_DIR"

# Migrate existing voice file into sync dir (first machine only)
if [ -f "$VOICES_DIR/voice_clone.wav" ] && [ ! -L "$VOICES_DIR/voice_clone.wav" ]; then
    if [ ! -f "$SYNC_VOICES/voice_clone.wav" ]; then
        cp "$VOICES_DIR/voice_clone.wav" "$SYNC_VOICES/voice_clone.wav"
        info "Moved voice_clone.wav into sync dir"
    fi
    rm "$VOICES_DIR/voice_clone.wav"
fi

# Create symlink (idempotent)
if [ ! -L "$VOICES_DIR/voice_clone.wav" ]; then
    ln -sf "$SYNC_VOICES/voice_clone.wav" "$VOICES_DIR/voice_clone.wav"
    info "Linked $VOICES_DIR/voice_clone.wav → $SYNC_VOICES/voice_clone.wav"
fi

# ── Systemd service ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/pocket-tts.service << SVCEOF
[Unit]
Description=Pocket TTS (Kyutai) - voice cloning TTS
After=network.target

[Service]
ExecStartPre=/bin/bash -c "fuser -k ${PORT}/tcp 2>/dev/null || true"
ExecStart=${VENV}/bin/pocket-tts serve --host 0.0.0.0 --port ${PORT} --quantize
Restart=always
RestartSec=5
MemoryMax=3G

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pocket-tts
systemctl start pocket-tts

# Wait up to 60s for model load
info "Waiting for model to load..."
for i in $(seq 1 30); do
    if curl -sf --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        success "Pocket TTS ready on :${PORT}"
        exit 0
    fi
    sleep 2
done
warn "Timeout waiting — check: journalctl -u pocket-tts -n 30"
exit 1
