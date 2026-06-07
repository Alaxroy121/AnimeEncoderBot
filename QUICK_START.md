# AnimeEncoderBot Pyrogram Migration - Quick Start

**TL;DR: Replace python-telegram-bot with Pyrogram + async FFmpeg**

## Step 1: Install Dependencies (2 minutes)

```bash
pip install pyrogram tgcrypto aiofiles
# Remove old framework
pip uninstall python-telegram-bot -y
```

## Step 2: Update requirements.txt

```bash
cat > requirements.txt << 'REQ'
pyrogram>=2.0.109
tgcrypto
aiofiles
motor
pymongo
python-dotenv
google-api-python-client
google-auth
Pillow
hachoir
REQ
```

## Step 3: Critical Config Changes (bot.py)

### Before (SLOW):
```python
from telegram.ext import Application

app = Application.builder().token(BOT_TOKEN).build()
app.run_polling()  # ❌ Inefficient polling
```

### After (FAST):
```python
from pyrogram import Client

app = Client(
    name='bot',
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=32,                        # ✅ 32 workers (default ~4)
    max_concurrent_transmissions=16,   # ✅ 16 parallel uploads (default 4)
    sleep_threshold=30,
    ipv6=False,
    proxy=None,
)

async def main():
    async with app:
        await app.idle()

if __name__ == '__main__':
    asyncio.run(main())
```

## Step 4: FFmpeg GPU Optimization (encoder.py)

### Before (CPU bottleneck):
```bash
ffmpeg -i input.mp4 -c:v libx265 -crf 24 output.mkv
# ❌ Uses CPU only, slooow
```

### After (GPU-accelerated):
```bash
ffmpeg -hwaccel cuda -hwaccel_output_format cuda \
  -i input.mp4 \
  -c:v hevc_nvenc \
  -preset p6 \
  -rc vbr \
  -cq 22 \
  -b:v 8M \
  -maxrate 16M \
  -c:a aac -b:a 192k \
  output.mkv
# ✅ GPU handles encoding, CPU handles I/O
```

## Step 5: Async Subprocess (DON'T BLOCK!)

### Before (Blocks event loop):
```python
result = subprocess.run(ffmpeg_cmd)  # ❌ Event loop frozen!
```

### After (Async):
```python
async def encode_async(ffmpeg_cmd):
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode

# Usage (in async handler):
result = await encode_async(ffmpeg_cmd)  # ✅ Event loop free!
```

## Step 6: Async File I/O

### Before:
```python
with open(file, 'rb') as f:\n    data = f.read()  # ❌ Blocks!\n```\n\n### After:
```python
import aiofiles

async def read_file(file):
    async with aiofiles.open(file, 'rb') as f:\n        data = await f.read()  # ✅ Non-blocking!\n    return data\n```\n\n## Step 7: Handler Pattern

### Before:
```python
@app.on_message(filters.command('start'))
async def start(client, message):
    await message.reply("Hi!")  # Works but inefficient
```

### After (fully async):
```python
@app.on_message(filters.command('start'))
async def start(client, message):
    # ✅ Pyrogram handles all async natively
    user_id = message.from_user.id
    
    # Download (async, doesn't block)
    file = await app.download_media(message, file_name='temp.mp4')
    
    # Process (async subprocess, doesn't block)
    await encode_async(file)
    
    # Upload (async, patched for 16x concurrency)
    await app.send_document(message.chat.id, 'output.mkv')
```

## Step 8: Verify GPU Usage

```bash
# Terminal 1: Watch GPU
watch -n 1 nvidia-smi

# Terminal 2: Watch CPU
top -u ubuntu

# Terminal 3: Run bot
python bot.py

# Send video to bot
# GPU should jump to 60-85%, CPU should stay 50-80%
```

### Expected Output:

```
GPU: 75% | Mem: 2.1/15GB (14%)  ✅ Good!
CPU: 62% (8 cores @ 60-70% each) ✅ Good!
```

### BAD (old code):
```
GPU: 5% | Mem: 0.5/15GB
CPU: 400% (all cores maxed)  ❌ Bad!
```

## Step 9: Copy Template Files

```bash
# Use the provided templates:
cp bot_v2_pyrogram.py bot.py
cp encoder_v2_async.py encoder.py

# Customize config in config.py:
CONCURRENT_TASKS = 2      # For T4 (single GPU)
# or
CONCURRENT_TASKS = 4      # For RTX/A100
```

## Step 10: Test

```bash
# Send /start command to bot
# Response should be instant

# Send video file
# GPU/CPU usage should show:
# - GPU: 60-85% ✅
# - CPU: 50-80% (not 400%!) ✅
```

## Troubleshooting

### "GPU: 5%, CPU: 400%" → Still using old code
- Check: `grep -r "subprocess.run" *.py`
- Replace with `asyncio.create_subprocess_exec`

### "ffmpeg: unknown encoder 'hevc_nvenc'"
- Run: `ffmpeg -encoders | grep nvenc`
- Install NVIDIA driver: `apt install nvidia-codec-headers`

### "Connection timeout" errors
- Increase `sleep_threshold=60` or `90`
- Check internet speed

### "Files in queue" but GPU idle
- Increase `CONCURRENT_TASKS` value
- Check task processor is truly async (no blocking I/O)

## Performance Checklist

- [ ] GPU usage: 60-85%
- [ ] CPU usage: 50-80%
- [ ] Encoding speed: 2x+ faster
- [ ] Multiple videos queued? All processing in parallel?
- [ ] Upload/download: 4+ files simultaneously?
- [ ] No "Event loop blocked" warnings in logs?

## Reference Files

- `/home/ubuntu/.openclaw/workspace/animeencoderbot/OPTIMIZATION_GUIDE.md` (Deep dive)
- `/home/ubuntu/.openclaw/workspace/animeencoderbot/bot_v2_pyrogram.py` (Template)
- `/home/ubuntu/.openclaw/workspace/animeencoderbot/encoder_v2_async.py` (GPU encoder)
- https://github.com/abhinai2244/Encoding-Bot (Full working reference)

---

**Estimated time to fix: 30 minutes** ⏱
- 5 min: Install Pyrogram
- 10 min: Update bot.py from template
- 10 min: Update encoder.py from template
- 5 min: Test with nvidia-smi

Good luck! 🚀
