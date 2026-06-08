# 🎬 AnimeEncoderBot

**Open-source Telegram bot for GPU-accelerated video encoding and AI anime upscaling.**

Encode videos in **AV1** or **H.265/HEVC** with NVIDIA GPU acceleration, and upscale anime to **1080p, 2K, 4K, or 8K** using Real-ESRGAN — all from Telegram.

---

## ✨ Features

- **Video Encoding** — AV1 (SVT-AV1) and H.265/HEVC (NVENC GPU + CPU fallback)
- **AI Anime Upscaling** — Real-ESRGAN with `realesr-animevideov3` model
- **Resolution Targets** — 1080p (1920×1080), 2K (2560×1440), 4K (3840×2160), 8K (7680×4320)
- **GPU Accelerated** — Optimized for NVIDIA T4 (Kaggle), RTX, GTX, and other NVIDIA GPUs
- **Quality Presets** — Low (fast), Medium, High, Ultra (slow)
- **Smart Queue** — Priority-based async task queue with concurrent workers
- **Progress Tracking** — Live progress bar, ETA, and speed in Telegram
- **Admin Tools** — Stats, broadcast, ban/unban, task logs
- **MongoDB Backend** — User data, task history, global statistics
- **Large File Support** — Auto-splits files >2GB for Telegram upload
- **Auto Retry** — Failed tasks retry automatically (configurable)

---

## 📋 Prerequisites

- **Python** 3.10+
- **FFmpeg** (with NVENC support for GPU encoding)
- **MongoDB** 6.0+ (local or Atlas)
- **NVIDIA GPU** + drivers (optional — falls back to CPU)
- **Real-ESRGAN** binary (for upscaling)
- **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
- **Telegram API credentials** from [my.telegram.org](https://my.telegram.org)

---

## 🚀 Installation

### Option 1: VPS (Ubuntu/Debian)

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/AnimeEncoderBot.git
cd AnimeEncoderBot

# Run the installer (as root)
sudo bash install.sh

# Edit your config
nano /opt/AnimeEncoderBot/config.env

# Start the bot
sudo systemctl start anime-encoder-bot

# Check logs
journalctl -u anime-encoder-bot -f
```

The installer automatically:
- Installs Python, FFmpeg, MongoDB, NVIDIA drivers, CUDA
- Downloads Real-ESRGAN with anime models
- Creates a Python virtual environment
- Sets up a systemd service

**Manual VPS setup (if you prefer):**

```bash
# System dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg git wget unzip

# Clone repo
git clone https://github.com/YOUR_USERNAME/AnimeEncoderBot.git
cd AnimeEncoderBot

# Virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install Real-ESRGAN
mkdir -p /opt/realesrgan
wget https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip -O /tmp/realesrgan.zip
unzip /tmp/realesrgan.zip -d /opt/realesrgan
chmod +x /opt/realesrgan/realesrgan-ncnn-vulkan
sudo ln -sf /opt/realesrgan/realesrgan-ncnn-vulkan /usr/local/bin/realesrgan-ncnn-vulkan

# Configure
cp config.env config.env.bak
nano config.env

# Run
python3 bot.py
```

---

### Option 2: Docker

**Prerequisites:** Docker + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

```bash
# Install NVIDIA Container Toolkit (if not already)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Clone and configure
git clone https://github.com/YOUR_USERNAME/AnimeEncoderBot.git
cd AnimeEncoderBot
nano config.env  # Fill in your credentials

# Set MongoDB URI for Docker
# In config.env, change MONGO_URI to:
# MONGO_URI=mongodb://mongo:27017/anime_encoder_bot

# Build and run
docker compose up -d

# Check logs
docker compose logs -f bot
```

**Docker commands:**
```bash
docker compose up -d       # Start
docker compose down        # Stop
docker compose restart bot # Restart bot only
docker compose logs -f     # Live logs
```

---

### Option 3: Google Kaggle (Free GPU)

Kaggle offers free Tesla T4 GPU sessions (up to 12 hours).

1. Create a Kaggle account at [kaggle.com](https://www.kaggle.com)
2. Create a **New Notebook**
3. Go to **Settings → Accelerator → GPU T4 x2**
4. Upload `kaggle_setup.ipynb` or copy its cells
5. Set up **MongoDB Atlas** (free tier) at [cloud.mongodb.com](https://cloud.mongodb.com)
   - Create a cluster → Get connection string
   - Whitelist `0.0.0.0/0` in Network Access
6. Fill in your credentials in the notebook
7. Run all cells

**Using Kaggle Secrets (recommended for security):**
- Go to Settings → Add-ons → Secrets
- Add: `BOT_TOKEN`, `API_ID`, `API_HASH`, `ADMIN_IDS`, `MONGO_URI`
- Use `kaggle_secrets.UserSecretsClient()` in the notebook

> ⚠️ Kaggle GPU sessions last max 12 hours. For 24/7 operation, use a VPS.

---

## ⚙️ Configuration

Edit `config.env` with your values:

| Variable | Description | Example |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather | `123456:ABC-DEF` |
| `API_ID` | Telegram API ID from my.telegram.org | `12345678` |
| `API_HASH` | Telegram API hash | `abcdef1234567890` |
| `ADMIN_IDS` | Admin Telegram user IDs (comma-separated) | `123456789,987654321` |
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017/anime_encoder_bot` |
| `LOG_CHANNEL` | Telegram channel ID for bot logs | `-1001234567890` |
| `DOWNLOAD_DIR` | Temp directory for downloads | `./downloads` |
| `MAX_FILE_SIZE` | Max input file size in bytes (default: 2GB) | `2147483648` |
| `DEFAULT_CODEC` | Default codec: `av1` or `hevc` | `hevc` |
| `GPU_ENABLED` | Enable GPU acceleration | `true` |
| `CONCURRENT_TASKS` | Max simultaneous tasks | `2` |
| `REALESRGAN_GPU_IDS` | GPU IDs for Real-ESRGAN upscaling (`auto` uses every detected GPU) | `auto` |
| `UPSCALE_PARALLEL_JOBS` | Parallel Real-ESRGAN segment jobs (`0` = one job per GPU) | `0` |
| `UPSCALE_SEGMENT_SECONDS` | Segment length for parallel upscaling; smaller starts GPU work sooner | `10` |
| `REALESRGAN_THREADS` | Real-ESRGAN load/process/save thread tuning | `2:4:2` |
| `REALESRGAN_OUTPUT_FORMAT` | Upscaled frame format; `jpg` is faster/lower disk than `png` | `jpg` |

---

## 🎮 Usage

### Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and system status |
| `/help` | Detailed help with all commands |
| `/encode` | Start encoding workflow (codec → quality → preset → audio → send file) |
| `/upscale` | Start upscaling workflow (resolution → send file) |
| `/status` | Check your current task progress |
| `/cancel` | Cancel your active task |
| `/queue` | View the task queue |
| `/settings` | Manage your default preferences |

### Admin Commands

| Command | Description |
|---|---|
| `/stats` | Bot statistics (users, tasks, data processed) |
| `/broadcast <msg>` | Send a message to all users |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/logs` | View recent task logs |

### Encoding Workflow
1. Send `/encode`
2. Choose codec: **AV1** or **HEVC**
3. Choose quality: **Low** / **Medium** / **High** / **Ultra**
4. Choose speed: **Fast** / **Medium** / **Slow** / **Very Slow**
5. Choose audio: **Copy** / **AAC** / **Opus**
6. Send your video file
7. Watch progress and receive the encoded file

### Upscaling Workflow
1. Send `/upscale`
2. Choose resolution: **1080p** / **2K** / **4K** / **8K**
3. Send your video file
4. Wait for AI upscaling (frame-by-frame Real-ESRGAN)
5. Receive the upscaled file

---

## 📁 Supported Formats

**Input:** MP4, MKV, AVI, MOV, WebM, FLV, WMV, M4V, TS, M2TS, VOB

**Output:**
- Encoding: MKV (with subtitles) or MP4
- Upscaling: MKV

---

## 🔧 Encoding Details

### HEVC (H.265)
- **GPU (NVENC):** Uses `hevc_nvenc` with B-frames, spatial/temporal AQ, 10-bit
- **CPU fallback:** Uses `libx265` with CRF-based quality control
- Optimized presets: P4 (fast) to P7 (max quality) for NVENC

### AV1
- Uses **SVT-AV1** (`libsvtav1`) — fast and efficient
- Presets 2 (ultra) to 8 (fast)
- 10-bit output, scene change detection

### Quality Tiers
| Tier | HEVC CRF | HEVC CQ (GPU) | AV1 CRF | Speed |
|---|---|---|---|---|
| Low | 30 | 32 | 38 | Fastest |
| Medium | 24 | 26 | 30 | Balanced |
| High | 20 | 22 | 24 | Slow |
| Ultra | 16 | 18 | 18 | Slowest |

---

## 🤖 AI Upscaling Details

- **Model:** `realesr-animevideov3` (optimized for anime content)
- **Pipeline:** Extract frames → Real-ESRGAN upscale → Reassemble with audio
- **Scale factors:** 2x, 3x, 4x (auto-calculated from input/target resolution)
- **Output codec:** H.265 CRF 16 (preserves upscaled quality)

---

## 🛠 Troubleshooting

**Bot doesn't start:**
- Check `config.env` has valid credentials
- Ensure MongoDB is running: `systemctl status mongod`
- Check logs: `journalctl -u anime-encoder-bot -f` or `docker compose logs bot`

**GPU not detected:**
- Verify drivers: `nvidia-smi`
- Check FFmpeg NVENC: `ffmpeg -encoders | grep nvenc`
- Bot auto-falls back to CPU if GPU isn't available

**Encoding is slow:**
- CPU encoding is much slower than GPU
- For AV1, use `fast` preset (SVT-AV1 preset 8)
- HEVC NVENC is the fastest option with GPU

**Upscaling fails:**
- Verify Real-ESRGAN: `realesrgan-ncnn-vulkan -h`
- Check available disk space (frames are extracted as PNG)
- 8K upscaling requires significant RAM and disk space

**Large files won't upload:**
- Telegram limit is ~2GB
- Bot auto-splits larger files
- Check `MAX_FILE_SIZE` in config

**MongoDB connection error:**
- Local: `systemctl start mongod`
- Atlas: Check whitelist and connection string
- Docker: Use `mongodb://mongo:27017/anime_encoder_bot`

---

## 📊 Project Structure

```
AnimeEncoderBot/
├── bot.py              # Main entry point, file handler, task processor
├── commands.py         # All /command handlers
├── callbacks.py        # Inline button callbacks, workflow state
├── encoder.py          # FFmpeg encoding engine (AV1, HEVC, GPU)
├── upscaler.py         # Real-ESRGAN anime upscaling pipeline
├── database.py         # MongoDB async operations
├── queue_manager.py    # Priority task queue with workers
├── utils.py            # Progress, media info, file helpers
├── config.py           # Environment config loader
├── config.env          # Configuration (not committed)
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker image (NVIDIA CUDA base)
├── docker-compose.yml  # Docker Compose with GPU + MongoDB
├── kaggle_setup.ipynb  # Kaggle deployment notebook
├── install.sh          # VPS auto-installer
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

---

## 📜 License

MIT License — use it, modify it, ship it. See [LICENSE](LICENSE) for details.

---

## 🙏 Credits

- [Pyrogram](https://github.com/pyrogram/pyrogram) — Telegram MTProto framework
- [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) — AI upscaling
- [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) — AV1 encoder
- [FFmpeg](https://ffmpeg.org/) — The backbone of everything
- [Motor](https://github.com/mongodb/motor) — Async MongoDB driver
