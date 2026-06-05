#!/usr/bin/env bash
# ============================================
# AnimeEncoderBot - VPS Installation Script
# Tested on: Ubuntu 22.04 / 24.04, Debian 12
# ============================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()  { echo -e "${CYAN}[i]${NC} $1"; }

# ── Check root ──
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

# ── Detect OS ──
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
    VER=$VERSION_ID
else
    error "Cannot detect OS. This script supports Ubuntu/Debian."
fi

info "Detected: $OS $VER"

if [[ "$OS" != "ubuntu" && "$OS" != "debian" ]]; then
    error "Unsupported OS: $OS. This script supports Ubuntu and Debian."
fi

echo ""
echo "============================================"
echo "  AnimeEncoderBot - Installation"
echo "============================================"
echo ""

# ── Step 1: System update ──
log "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# ── Step 2: Essential packages ──
log "Installing essential packages..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git wget curl unzip \
    build-essential pkg-config \
    libvulkan1 vulkan-tools \
    gnupg ca-certificates

# ── Step 3: FFmpeg ──
log "Installing FFmpeg..."
apt-get install -y -qq ffmpeg
FFMPEG_VER=$(ffmpeg -version 2>/dev/null | head -1 || echo "unknown")
info "FFmpeg: $FFMPEG_VER"

# ── Step 4: NVIDIA drivers & CUDA ──
info "Checking for NVIDIA GPU..."
if lspci 2>/dev/null | grep -qi nvidia; then
    log "NVIDIA GPU detected!"

    if ! command -v nvidia-smi &>/dev/null; then
        log "Installing NVIDIA drivers..."
        apt-get install -y -qq nvidia-driver-535 nvidia-utils-535
        warn "A REBOOT may be required for GPU drivers to load."
    fi

    if ! dpkg -l | grep -q cuda-toolkit; then
        log "Installing CUDA toolkit..."
        wget -q https://developer.download.nvidia.com/compute/cuda/repos/${OS}$(echo $VER | tr -d '.')/x86_64/cuda-keyring_1.1-1_all.deb \
            -O /tmp/cuda-keyring.deb 2>/dev/null || true
        if [[ -f /tmp/cuda-keyring.deb ]]; then
            dpkg -i /tmp/cuda-keyring.deb
            apt-get update -qq
            apt-get install -y -qq cuda-toolkit-12-2
            rm /tmp/cuda-keyring.deb
        else
            warn "Could not auto-install CUDA. Install manually:"
            warn "  https://developer.nvidia.com/cuda-downloads"
        fi
    fi

    if command -v nvidia-smi &>/dev/null; then
        info "GPU info:"
        nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    fi
else
    warn "No NVIDIA GPU detected. Bot will run in CPU-only mode."
    warn "Encoding will be slower without GPU acceleration."
fi

# ── Step 5: MongoDB ──
log "Installing MongoDB..."
if ! command -v mongod &>/dev/null; then
    wget -qO - https://www.mongodb.org/static/pgp/server-7.0.asc | \
        gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg

    if [[ "$OS" == "ubuntu" ]]; then
        echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
            > /etc/apt/sources.list.d/mongodb-org-7.0.list
    else
        echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" \
            > /etc/apt/sources.list.d/mongodb-org-7.0.list
    fi

    apt-get update -qq
    apt-get install -y -qq mongodb-org
fi

systemctl enable mongod
systemctl start mongod
log "MongoDB is running."

# ── Step 6: Real-ESRGAN ──
log "Installing Real-ESRGAN..."
REALESRGAN_DIR="/opt/realesrgan"
if [[ ! -f "$REALESRGAN_DIR/realesrgan-ncnn-vulkan" ]]; then
    mkdir -p "$REALESRGAN_DIR"
    wget -q https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
        -O /tmp/realesrgan.zip
    unzip -o /tmp/realesrgan.zip -d "$REALESRGAN_DIR"
    chmod +x "$REALESRGAN_DIR/realesrgan-ncnn-vulkan"
    ln -sf "$REALESRGAN_DIR/realesrgan-ncnn-vulkan" /usr/local/bin/realesrgan-ncnn-vulkan
    rm /tmp/realesrgan.zip
    log "Real-ESRGAN installed at $REALESRGAN_DIR"
else
    info "Real-ESRGAN already installed."
fi

# ── Step 7: Bot setup ──
BOT_DIR="/opt/AnimeEncoderBot"
log "Setting up bot in $BOT_DIR..."

if [[ -d "$BOT_DIR" ]]; then
    warn "Bot directory exists. Updating..."
    cd "$BOT_DIR"
else
    mkdir -p "$BOT_DIR"
    # Copy files from current directory if running from the repo
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [[ -f "$SCRIPT_DIR/bot.py" ]]; then
        cp -r "$SCRIPT_DIR"/* "$BOT_DIR/"
    else
        warn "bot.py not found in script directory."
        warn "Clone the repo to $BOT_DIR manually."
    fi
    cd "$BOT_DIR"
fi

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate

# Create downloads directory
mkdir -p "$BOT_DIR/downloads"

# ── Step 8: Config check ──
if [[ ! -f "$BOT_DIR/config.env" ]]; then
    warn "config.env not found! Copy from template and fill in your values:"
    warn "  cp config.env.example config.env"
    warn "  nano config.env"
else
    info "config.env found. Make sure it's configured!"
fi

# ── Step 9: Systemd service ──
log "Creating systemd service..."
cat > /etc/systemd/system/anime-encoder-bot.service << 'EOF'
[Unit]
Description=AnimeEncoderBot - Telegram Video Encoder
After=network.target mongod.service
Wants=mongod.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AnimeEncoderBot
ExecStart=/opt/AnimeEncoderBot/venv/bin/python3 bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment
Environment=PYTHONUNBUFFERED=1

# Resource limits
LimitNOFILE=65535
MemoryMax=4G

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anime-encoder-bot
log "Systemd service created: anime-encoder-bot"

# ── Done ──
echo ""
echo "============================================"
echo -e "  ${GREEN}Installation Complete!${NC}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit config:   nano $BOT_DIR/config.env"
echo "  2. Start the bot:  systemctl start anime-encoder-bot"
echo "  3. Check logs:     journalctl -u anime-encoder-bot -f"
echo "  4. Check status:   systemctl status anime-encoder-bot"
echo ""
echo "Quick commands:"
echo "  systemctl start anime-encoder-bot    # Start"
echo "  systemctl stop anime-encoder-bot     # Stop"
echo "  systemctl restart anime-encoder-bot  # Restart"
echo "  journalctl -u anime-encoder-bot -f   # Live logs"
echo ""
if lspci 2>/dev/null | grep -qi nvidia; then
    warn "If you just installed NVIDIA drivers, REBOOT before starting the bot!"
fi
