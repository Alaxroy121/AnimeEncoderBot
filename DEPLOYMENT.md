# AnimeEncoderBot Deployment Guide (Pyrogram MTProto Optimized)

## 📍 Project Location
```
/home/ubuntu/.openclaw/workspace/animeencoderbot/
```

## 📂 Project Structure

### Documentation (Start Here!)
```
├── SUMMARY.md                    ← Quick overview (5 min read)
├── QUICK_START.md                ← Implementation guide (30 min)
├── OPTIMIZATION_GUIDE.md         ← Technical deep dive (1 hour)
├── PYROGRAM_MIGRATION.md         ← Quick reference card
└── FILES_CREATED.txt             ← This work's summary
```

### Core Application
```
├── bot_v2_pyrogram.py            ← OPTIMIZED bot template (COPY THIS)
├── encoder_v2_async.py           ← OPTIMIZED async encoder (COPY THIS)
│
├── bot.py                        ← Current implementation (python-telegram-bot)
├── encoder.py                    ← Current encoder (subprocess blocking)
├── callbacks.py                  ← Callback handlers
├── commands.py                   ← Command handlers
├── config.py                     ← Configuration
├── database.py                   ← MongoDB operations
├── gdrive.py                     ← Google Drive integration
├── queue_manager.py              ← Task queue management
├── upscaler.py                   ← Real-ESRGAN upscaling
└── utils.py                      ← Utility functions
```

### Configuration & Deployment
```
├── requirements.txt              ← Python dependencies
├── config.env                    ← Environment variables (bot token, etc.)
├── kaggle_setup.ipynb            ← Kaggle notebook for T4 GPU
├── Dockerfile                    ← Docker image definition
├── docker-compose.yml            ← Docker Compose setup
├── install.sh                    ← VPS auto-installer
└── assets/                       ← Welcome images
    ├── welcome_1.png
    ├── welcome_2.png
    ├── welcome_3.png
    ├── welcome_4.png
    └── welcome_5.png
```

### Git Repository
```
.git/                             ← GitHub repository (cloned from Alaxroy121)
```

---

## 🚀 Quick Start (30 Minutes)

### Step 1: Install Pyrogram (2 min)
```bash
cd /home/ubuntu/.openclaw/workspace/animeencoderbot
pip install pyrogram tgcrypto aiofiles
pip uninstall python-telegram-bot -y
```

### Step 2: Update Bot (1 min)
```bash
cp bot_v2_pyrogram.py bot.py
```

### Step 3: Update Encoder (1 min)
```bash
cp encoder_v2_async.py encoder.py
```

### Step 4: Configure (2 min)
Edit `config.py`:
```python
# For T4 GPU (Kaggle):
CONCURRENT_TASKS = 2

# For RTX 3090 / A100:
CONCURRENT_TASKS = 4
```

### Step 5: Verify FFmpeg NVENC (2 min)
```bash
ffmpeg -encoders | grep nvenc
# Should show: hevc_nvenc, av1_nvenc
```

### Step 6: Test (5 min)
**Terminal 1: Monitor GPU**
```bash
watch -n 1 nvidia-smi
```

**Terminal 2: Monitor CPU**
```bash
top -u ubuntu
```

**Terminal 3: Run Bot**
```bash
python bot.py
```

### Step 7: Send Video (5 min)
- Open Telegram
- Send /start to your bot
- Send a video file
- Watch GPU/CPU usage

**Expected:**
- GPU: 60-85% ✅
- CPU: 50-80% (not 400%!) ✅

---

## 📊 Performance Expectations

### Before Optimization (Current)
```
CPU: 400% (all cores maxed)
GPU: 5% (unused)
Concurrent tasks: 1-2
Encoding speed: Baseline
```

### After Optimization (Pyrogram)
```
CPU: 50-80% (balanced)
GPU: 60-85% (fully utilized)
Concurrent tasks: 4-8+
Encoding speed: 2-3x faster
```

---

## 🔧 Configuration Tuning

### For T4 GPU (Kaggle Notebooks)
```python
# config.py
CONCURRENT_TASKS = 2
CONCURRENT_UPLOADS = 4
CONCURRENT_DOWNLOADS = 4
ENCODING_THREADS = 4
FFMPEG_BUFFER = '32M'
TASK_TIMEOUT = 3600
MAX_RETRIES = 2
```

### For RTX 3090 / A100
```python
CONCURRENT_TASKS = 4
CONCURRENT_UPLOADS = 8
CONCURRENT_DOWNLOADS = 8
ENCODING_THREADS = 8
FFMPEG_BUFFER = '128M'
```

### For Kaggle Multi-GPU (T4 x2)
```python
CONCURRENT_TASKS = 3
CUDA_VISIBLE_DEVICES = '0,1'  # Both GPUs
```

---

## 🐳 Docker Deployment

### Build
```bash
docker-compose build
```

### Run
```bash
docker-compose up -d
```

### Monitor
```bash
docker-compose logs -f bot
```

### Stop
```bash
docker-compose down
```

---

## ☁️ Kaggle Deployment (Free T4 GPU)

### Step 1: Go to kaggle.com
- Create account / login
- Go to Notebooks

### Step 2: Create New Notebook
- Click "New Notebook"
- Go to Settings → Accelerator → GPU T4 x2
- Enable Persistent Sessions (if available)

### Step 3: Add Secrets
- Settings → Add-ons → Secrets
- Add these values:
  - `BOT_TOKEN` → from @BotFather
  - `API_ID` → from my.telegram.org
  - `API_HASH` → from my.telegram.org
  - `ADMIN_IDS` → your Telegram user ID (e.g., 123456789)
  - `MONGO_URI` → MongoDB Atlas connection string
  - `LOG_CHANNEL` → Telegram channel ID (optional)

### Step 4: Upload & Run
- Upload `kaggle_setup.ipynb` from repo
- Or copy cells from notebook
- **Run All**
- Bot will start automatically

---

## 🔐 Environment Variables (config.env)

```env
# Telegram
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
BOT_TOKEN=YOUR_BOT_TOKEN

# Users & Permissions
ADMIN_IDS=123456789,987654321
LOG_CHANNEL=-1001234567890

# Database
MONGO_URI=mongodb://localhost:27017/anime_encoder_bot

# Or MongoDB Atlas:
# MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/anime_encoder_bot

# Paths
DOWNLOAD_DIR=./downloads
ENCODE_DIR=./encoded

# GPU Settings
GPU_ENABLED=true
CUDA_VISIBLE_DEVICES=0

# Queue & Concurrency
CONCURRENT_TASKS=2
MAX_FILE_SIZE=2147483648  # 2GB
```

---

## 📝 Key Files to Understand

### bot_v2_pyrogram.py (302 lines)
- Pyrogram Client initialization
- Handler decorators (@app.on_message, @app.on_callback_query)
- Async task processor
- User state management

### encoder_v2_async.py (236 lines)
- Async FFmpeg encoder
- GPU detection (CUDA, NVENC, AV1)
- Quality presets
- Async subprocess (critical!)

### config.py
- All configuration constants
- Tweak CONCURRENT_TASKS here

---

## 🛠 Troubleshooting

### "GPU: 5%, CPU: 400%" (Still slow)
**Check:** Is bot still using `subprocess.run()`?
```bash
grep -r "subprocess.run" *.py
```
**Fix:** Replace with `asyncio.create_subprocess_exec()`

### "ffmpeg: unknown encoder 'hevc_nvenc'"
**Check:** Does FFmpeg support NVENC?
```bash
ffmpeg -encoders | grep nvenc
```
**Fix:** Install NVIDIA drivers
```bash
apt-get install nvidia-codec-headers
```

### "Connection timeout" in Telegram
**Fix:** Increase sleep_threshold in Pyrogram config
```python
sleep_threshold=60  # or 90
```

### "Files in queue but GPU idle"
**Check:** Is CONCURRENT_TASKS too low?
**Fix:** Increase in config.py

### "Memory usage increasing"
**Check:** Are temp files being cleaned up?
**Fix:** Add cleanup code in task processor

---

## 📚 Learning Resources

- **Pyrogram Docs:** https://docs.pyrogram.org
- **FFmpeg NVENC:** https://developer.nvidia.com/blog/nvidia-accelerated-video-encoding-ffmpeg/
- **Reference Bot:** https://github.com/abhinai2244/Encoding-Bot
- **NVIDIA GPU Docs:** https://docs.nvidia.com/cuda/

---

## ✅ Pre-Deployment Checklist

- [ ] `pip install pyrogram tgcrypto aiofiles`
- [ ] Copied `bot_v2_pyrogram.py` → `bot.py`
- [ ] Copied `encoder_v2_async.py` → `encoder.py`
- [ ] Updated `config.py` with CONCURRENT_TASKS
- [ ] Set up `config.env` with bot token & API credentials
- [ ] MongoDB ready (local or Atlas)
- [ ] FFmpeg installed with NVENC support
- [ ] GPU driver installed (nvidia-smi works)
- [ ] Python 3.10+ installed
- [ ] Virtual environment active (if using one)

---

## 🎯 Next Steps

1. **Read:** SUMMARY.md or QUICK_START.md
2. **Copy:** Templates (bot_v2_pyrogram.py, encoder_v2_async.py)
3. **Configure:** config.py and config.env
4. **Install:** `pip install pyrogram tgcrypto aiofiles`
5. **Test:** `python bot.py` with `nvidia-smi` monitoring
6. **Deploy:** Docker or Kaggle when ready

---

## 💬 Support

- Check **QUICK_START.md** troubleshooting first
- Read **OPTIMIZATION_GUIDE.md** for technical details
- Check logs: `tail -f bot.log`
- Monitor: `watch -n 1 nvidia-smi`

---

**Status: ✅ Ready for Deployment**

All optimization files created. Templates ready to use.
~30 minutes to full GPU acceleration! 🚀

Good luck! 🦞
