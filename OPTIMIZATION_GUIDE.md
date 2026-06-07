# AnimeEncoderBot CPU/GPU Optimization Guide

## Problem Analysis: 400% CPU, 5% GPU

### Current Issues

1. **Framework Mismatch**: Using `python-telegram-bot` (sync-based with asyncio wrapper) instead of pure **Pyrogram** (native MTProto async)
   - Pyrogram is built for async from the ground up
   - MTProto allows better connection pooling and concurrent operations
   - No overhead from sync→async bridges

2. **Upload/Download Bottleneck**
   - Default Pyrogram config: `max_concurrent_transmissions=4`
   - Reference bot (Encoding-Bot) uses: `max_concurrent_transmissions=16`
   - This limits parallel file uploads—forces sequential transfers

3. **Worker Pool Undersized**
   - Current: default Pyrogram workers (likely 4-8)
   - Reference bot: `workers=32`
   - More workers = better async event loop utilization

4. **FFmpeg GPU Encoding Underutilized**
   - Missing GPU-specific flags for concurrent encoding
   - No CUDA decode (cuvid) for input decoding
   - No proper preset/CRF tuning for GPU load distribution

5. **Blocking Subprocess Calls**
   - FFmpeg runs as `subprocess.run()` or `subprocess.Popen()` 
   - Should use `asyncio.create_subprocess_exec()` instead
   - Current approach blocks the event loop

6. **Task Queue Processing**
   - If queue workers are synchronous or don't properly await, CPU will spike
   - GPU sits idle while CPU waits for I/O (Telegram transfers)

7. **File I/O Not Async**
   - Reading/writing encoded files blocks event loop
   - Should use `aiofiles` for async file I/O

---

## Solution: Pyrogram-Native MTProto Implementation

### 1. Migrate from python-telegram-bot to Pyrogram

**Before (Current - Problematic):**
```python
from telegram import Update
from telegram.ext import Application, CommandHandler

app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler('start', start))
app.run_polling()
```

**After (Pyrogram - Fixed):**
```python
from pyrogram import Client
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command

app = Client(
    session_name='bot',
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    # ✅ KEY CONFIGS FOR CPU/GPU:
    workers=32,                           # More workers for event loop
    max_concurrent_transmissions=16,      # Parallel file transfers (default=4!)
    sleep_threshold=30,                   # Prevent rate limiting
    ipv6=False,                          # IPv4 only (more stable)
    proxy=None,                          # No proxy overhead
)

@app.on_message(filters=command('start'))
async def start_handler(client, message):
    await message.reply("Hi!")

app.run()
```

---

### 2. Optimize FFmpeg GPU Encoding

**HEVC NVENC Optimized Flags:**
```python
# Instead of generic -c:v hevc_nvenc, use:
ffmpeg_cmd = [
    'ffmpeg',
    '-hwaccel', 'cuda',           # ✅ GPU decode (CUVID)
    '-hwaccel_output_format', 'cuda',
    '-i', input_file,
    '-c:v', 'hevc_nvenc',         # GPU encode
    '-preset', 'slow',            # P7 = best quality, P4 = fast
    '-rc', 'vbr',                 # Variable bitrate (better quality)
    '-cq', '22',                  # Quality level (lower=better, 0-51)
    '-b:v', '8M',                 # Target bitrate
    '-maxrate', '16M',
    '-bufsize', '16M',
    # ✅ Multi-GPU support:
    '-gpu', '0',                  # GPU selection (for multi-GPU systems)
    '-c:a', 'aac',
    '-b:a', '192k',
    output_file
]
```

**AV1 NVENC (if available):**
```python
ffmpeg_cmd = [
    'ffmpeg',
    '-hwaccel', 'cuda',
    '-hwaccel_output_format', 'cuda',
    '-i', input_file,
    '-c:v', 'av1_nvenc',
    '-preset', 'slow',            # P7 for AV1
    '-rc', 'vbr',
    '-cq', '24',
    '-b:v', '6M',
    '-maxrate', '12M',
    output_file
]
```

---

### 3. Async FFmpeg Subprocess (Critical!)

**Before (Blocks event loop):**
```python
result = subprocess.run(ffmpeg_cmd, capture_output=True)
# ❌ Event loop blocked while encoding happens
```

**After (Async):**
```python
async def encode_async(input_file, output_file, ffmpeg_cmd):
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode

# Usage:
await encode_async(input_file, output_file, ffmpeg_cmd)
# ✅ Event loop can process other tasks
```

---

### 4. Async File I/O with aiofiles

**Before (Blocks):**
```python
with open(file, 'rb') as f:\n    data = f.read()\n# ❌ Blocks event loop\n```\n\n**After (Async):**
```python
import aiofiles

async def read_file(file):
    async with aiofiles.open(file, 'rb') as f:\n        data = await f.read()\n    return data\n\n# ✅ Doesn't block event loop\n```\n\n---\n\n### 5. Concurrent Upload/Download with Pyrogram Patch\n\nThe reference bot includes `pyrogram_patch.py` that optimizes file uploads:

```python
# From: VideoEncoder/utils/pyrogram_patch.py

async def save_file(self, path, file_id=None, file_part=0, progress=None):
    """Patched save_file with concurrent upload workers."""
    
    # ✅ Use max_concurrent_transmissions instead of hardcoded 4
    workers_count = self.max_concurrent_transmissions if is_big else 1
    
    # Create multiple workers to upload file chunks in parallel
    workers = [self.loop.create_task(worker(session)) for _ in range(workers_count)]
    queue = asyncio.Queue(1)
    
    # Upload chunks concurrently
    while True:
        chunk = fp.read(part_size)
        if not chunk:
            break
        await queue.put(chunk)  # Workers pick up chunks and upload
        file_part += 1

# Apply patch to Pyrogram
SaveFile.save_file = save_file
pyrogram.Client.save_file = save_file
```

**To use:**
1. Copy `VideoEncoder/utils/pyrogram_patch.py` from reference bot
2. Import it in your `__init__.py`: `from .pyrogram_patch import *`
3. This automatically patches Pyrogram's upload method

---

### 6. Queue Manager Async Optimization

**Key pattern:**
```python
async def _worker(self, worker_id):
    """Process tasks from queue."""
    while self._running:
        task = await self._queue.get()
        async with self._semaphore:
            try:
                # ✅ Uses async subprocess, async file I/O
                await self._process_fn(task)
            except Exception as e:\n                await self._handle_failure(task, e)\n            finally:
                self._queue.task_done()

# Concurrent operations:
async def process_task(task: Task):
    # Download (async)
    file = await client.download_media(task.input_file)
    
    # Encode (async subprocess, doesn't block)
    await encode_async(file, output_file, ffmpeg_cmd)
    
    # Upload (async, uses patched save_file with concurrency)
    await client.send_document(
        chat_id,
        document=output_file,
        progress=progress_callback
    )
```

---

### 7. Config Optimization for Your Hardware

**For T4 GPU (Kaggle) - optimize for VRAM limits:**
```python
# config.py
class Config:
    # GPU settings
    GPU_ENABLED = True
    CUDA_VISIBLE_DEVICES = '0'  # Single T4
    
    # Queue & concurrency
    CONCURRENT_TASKS = 2           # T4 can handle 2 concurrent encodes
    CONCURRENT_UPLOADS = 4          # Telegram transfers
    CONCURRENT_DOWNLOADS = 4
    
    # FFmpeg optimization
    ENCODING_THREADS = 4           # Per encoding job
    FFMPEG_BUFFER = '32M'          # Input buffer
    
    # Task management
    TASK_TIMEOUT = 3600            # 1 hour max
    MAX_RETRIES = 2
    QUEUE_SIZE = 100
```

**For RTX 3090 / A100 (more VRAM):**
```python
class Config:
    CONCURRENT_TASKS = 4           # 4 parallel encodes
    CONCURRENT_UPLOADS = 8
    ENCODING_THREADS = 8
    FFMPEG_BUFFER = '128M'
```

---

### 8. CPU/GPU Monitoring

**Add real-time monitoring:**
```python
import psutil
import subprocess

async def monitor_resources():
    """Log CPU/GPU usage."""
    while True:
        cpu = psutil.cpu_percent(interval=1)
        gpu = subprocess.check_output(
            'nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits',
            shell=True
        ).decode().strip()
        
        logger.info(f"CPU: {cpu}% | GPU: {gpu}%")
        await asyncio.sleep(10)

# Start monitoring task
asyncio.create_task(monitor_resources())
```

---

## Implementation Checklist

- [ ] Replace `python-telegram-bot` with `pyrogram`
- [ ] Configure: `workers=32`, `max_concurrent_transmissions=16`
- [ ] Apply pyrogram upload patch from reference bot
- [ ] Use `asyncio.create_subprocess_exec()` for FFmpeg
- [ ] Add `aiofiles` for file I/O
- [ ] Optimize FFmpeg flags: `-hwaccel cuda`, `-c:v hevc_nvenc`, `-preset slow`
- [ ] Implement async task queue workers
- [ ] Add resource monitoring (CPU/GPU)
- [ ] Test with multiple concurrent uploads (stress test)
- [ ] Tune `CONCURRENT_TASKS` based on GPU VRAM

---

## Expected Results

| Metric | Before | After |
|--------|--------|-------|
| CPU Usage | 400% | 50-80% |
| GPU Usage | 5% | 60-85% |
| Concurrent Tasks | 1-2 | 4-8+ |
| Upload Speed | Sequential | Parallel (16x) |
| Event Loop Blocked | Frequent | Rare |
| Files in Queue | Backed up | Flowing |

---

## References

- **Pyrogram Docs**: https://docs.pyrogram.org
- **Reference Bot**: https://github.com/abhinai2244/Encoding-Bot
- **FFmpeg NVENC**: https://developer.nvidia.com/blog/nvidia-accelerated-video-encoding-ffmpeg/
- **Asyncio Best Practices**: https://realpython.com/async-io-python/

---

**Next Step**: Apply patches one at a time and monitor with `nvidia-smi` + `top`.
