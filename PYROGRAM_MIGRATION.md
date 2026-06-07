# AnimeEncoderBot - Pyrogram MTProto Optimization Summary

## Problem
- **CPU:** 400% (4 cores maxed out)
- **GPU:** 5% (unused)
- **Issue:** Using `python-telegram-bot` (sync-based) instead of Pyrogram (async MTProto)

## Root Causes
1. Sync framework wrapped with asyncio → overhead + blocking
2. FFmpeg subprocess blocks event loop
3. File I/O (read/write) blocks event loop
4. Upload/download limited to 4 concurrent (default Pyrogram)
5. Worker pool too small

## Solution: Pyrogram MTProto Native

### 3 Key Files Created

#### 1. **OPTIMIZATION_GUIDE.md** (9.5 KB)
Complete technical guide with:
- Problem analysis (7 issues identified)
- FFmpeg GPU optimization flags (HEVC NVENC, AV1 NVENC)
- Async patterns (subprocess, file I/O)
- Pyrogram patch for concurrent uploads
- Config optimization for T4/RTX GPUs
- Resource monitoring code
- Implementation checklist

#### 2. **bot_v2_pyrogram.py** (10.4 KB)
Pyrogram-native bot template with:
```python
# Critical configs:
workers=32                           # vs default ~4
max_concurrent_transmissions=16      # vs default 4
```
- Full async handlers
- Async task processor
- Connection pooling
- Reference implementation

#### 3. **encoder_v2_async.py** (8.5 KB)
Async FFmpeg encoder with:
- GPU detection (CUDA, NVENC, AV1)
- HEVC NVENC: `-preset p7 -rc vbr -cq 22`
- AV1 NVENC: `-preset p7 -rc vbr -cq 18`
- **Async subprocess** (doesn't block event loop)
- Quality presets (low/medium/high/ultra)
- Fallback to CPU if GPU unavailable

### Implementation Checklist

**Phase 1: Framework Migration**
- [ ] Uninstall `python-telegram-bot`
- [ ] Install `pyrogram`, `tgcrypto`, `aiofiles`
- [ ] Copy bot_v2_pyrogram.py → bot.py
- [ ] Test basic /start command

**Phase 2: GPU Encoding**
- [ ] Copy encoder_v2_async.py → encoder.py
- [ ] Update FFmpeg flags in encoder_v2_async.py
- [ ] Test encoding: `nvidia-smi` should show 60%+ GPU usage

**Phase 3: Concurrent Uploads**
- [ ] Copy pyrogram_patch.py from reference bot
- [ ] Import patch in bot.py
- [ ] Enable `max_concurrent_transmissions=16`

**Phase 4: Async File I/O**
- [ ] Replace `open()` with `aiofiles`
- [ ] Replace `subprocess.run()` with `asyncio.create_subprocess_exec()`
- [ ] Test with `top` + `nvidia-smi` side-by-side

**Phase 5: Monitoring**
- [ ] Add resource monitoring (CPU/GPU %)
- [ ] Log metrics every 10 seconds
- [ ] Stress test: 4 concurrent uploads + 2 encodings

## Expected Performance Gains

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| CPU Usage | 400% | 50-80% | -80% |
| GPU Usage | 5% | 60-85% | +1600% |
| Concurrent Tasks | 1-2 | 4-8 | +400% |
| Upload Throughput | Sequential | Parallel (16x) | +1200% |
| Event Loop Blocks | Frequent | Rare | Near zero |

## Reference Bot Architecture

**Encoding-Bot** (https://github.com/abhinai2244/Encoding-Bot):
- Pyrogram: `workers=32`, `max_concurrent_transmissions=16`
- Custom pyrogram_patch.py for upload concurrency
- Async subprocess for FFmpeg
- Async task queue with worker pool
- Proper logging and error handling

## Key Takeaways

1. **Framework Matters**: Pyrogram (async native) >> python-telegram-bot (sync + asyncio wrapper)
2. **Concurrency**: Default Pyrogram settings are very conservative. Must increase workers + transmissions.
3. **GPU**: FFmpeg needs `-hwaccel cuda` (decode) and `-c:v hevc_nvenc` (encode) to actually use GPU
4. **Async I/O**: Every blocking operation (file read, subprocess.run, file write) steals GPU time
5. **Monitoring**: Must check `nvidia-smi` + `top` simultaneously to catch hidden blocking

## Next Steps

1. Read `OPTIMIZATION_GUIDE.md` thoroughly
2. Start with Phase 1 (framework migration)
3. Test each phase independently
4. Monitor CPU/GPU usage with: `watch -n 1 nvidia-smi` + `top -u ubuntu`
5. Adjust `CONCURRENT_TASKS` based on GPU VRAM

---

**Files Created:**
- `/home/ubuntu/.openclaw/workspace/animeencoderbot/OPTIMIZATION_GUIDE.md` (Complete technical guide)
- `/home/ubuntu/.openclaw/workspace/animeencoderbot/bot_v2_pyrogram.py` (Pyrogram template)
- `/home/ubuntu/.openclaw/workspace/animeencoderbot/encoder_v2_async.py` (Async GPU encoder)

**Reference:** https://github.com/abhinai2244/Encoding-Bot (full working implementation)
