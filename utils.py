"""
Utility functions for AnimeEncoderBot.
Progress tracking, file handling, mediainfo, formatting helpers.
"""

import asyncio
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


# ── Formatting Helpers ────────────────────────────────────────────────

def human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    i = min(i, len(units) - 1)
    value = size_bytes / (1024 ** i)
    return f"{value:.2f} {units[i]}"


def format_duration(seconds: float) -> str:
    """Format seconds into HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def progress_bar(percent: float, length: int = 20) -> str:
    """Generate a text-based progress bar."""
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"


def format_eta(seconds: float) -> str:
    """Format ETA from seconds."""
    if seconds <= 0 or seconds > 86400:
        return "calculating..."
    return format_duration(seconds)


# ── MediaInfo Extraction ─────────────────────────────────────────────

async def get_media_info(file_path: str) -> Optional[dict]:
    """Extract media information using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("ffprobe failed for %s: %s", file_path, stderr.decode())
            return None

        data = json.loads(stdout.decode())
        info: dict = {
            "format": data.get("format", {}).get("format_long_name", "Unknown"),
            "duration": float(data.get("format", {}).get("duration", 0)),
            "size": int(data.get("format", {}).get("size", 0)),
            "bitrate": int(data.get("format", {}).get("bit_rate", 0)),
            "video": None,
            "audio": None,
            "subtitles": [],
        }

        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            if codec_type == "video" and info["video"] is None:
                info["video"] = {
                    "codec": stream.get("codec_name", "unknown"),
                    "width": int(stream.get("width", 0)),
                    "height": int(stream.get("height", 0)),
                    "fps": _parse_fps(stream.get("r_frame_rate", "0/1")),
                    "pix_fmt": stream.get("pix_fmt", "unknown"),
                }
            elif codec_type == "audio" and info["audio"] is None:
                info["audio"] = {
                    "codec": stream.get("codec_name", "unknown"),
                    "channels": int(stream.get("channels", 0)),
                    "sample_rate": int(stream.get("sample_rate", 0)),
                    "bitrate": int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else 0,
                }
            elif codec_type == "subtitle":
                info["subtitles"].append({
                    "codec": stream.get("codec_name", "unknown"),
                    "language": stream.get("tags", {}).get("language", "und"),
                })

        return info

    except Exception as e:
        logger.error("Error extracting media info: %s", e)
        return None


def _parse_fps(fps_str: str) -> float:
    """Parse FFmpeg frame rate string like '24000/1001'."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return round(float(num) / float(den), 3)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0


def format_media_info(info: dict) -> str:
    """Format media info dict into a readable string."""
    lines = ["📋 **Media Info**\n"]

    lines.append(f"📦 Format: `{info['format']}`")
    lines.append(f"⏱ Duration: `{format_duration(info['duration'])}`")
    lines.append(f"💾 Size: `{human_size(info['size'])}`")

    if info.get("video"):
        v = info["video"]
        lines.append(f"\n🎬 **Video**")
        lines.append(f"  Codec: `{v['codec']}`")
        lines.append(f"  Resolution: `{v['width']}×{v['height']}`")
        lines.append(f"  FPS: `{v['fps']}`")

    if info.get("audio"):
        a = info["audio"]
        lines.append(f"\n🔊 **Audio**")
        lines.append(f"  Codec: `{a['codec']}`")
        lines.append(f"  Channels: `{a['channels']}`")

    if info.get("subtitles"):
        langs = ", ".join(s["language"] for s in info["subtitles"])
        lines.append(f"\n💬 Subtitles: `{langs}`")

    return "\n".join(lines)


# ── File Handling ─────────────────────────────────────────────────────

def get_output_path(input_path: str, suffix: str, ext: str = ".mkv") -> str:
    """Generate output file path with suffix."""
    p = Path(input_path)
    stem = p.stem
    out_name = f"{stem}_{suffix}{ext}"
    return str(p.parent / out_name)


def is_within_tg_limit(file_path: str) -> bool:
    """Check if a file is small enough for direct Telegram upload (<2GB)."""
    return os.path.getsize(file_path) <= Config.TG_UPLOAD_LIMIT


def cleanup_files(*paths: str) -> None:
    """Remove files and directories safely."""
    for path in paths:
        try:
            p = Path(path)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as e:
            logger.warning("Failed to clean up %s: %s", path, e)


def is_supported_video(filename: str) -> bool:
    """Check if file extension is a supported video format."""
    return Path(filename).suffix.lower() in Config.SUPPORTED_EXTENSIONS


# ── Progress Tracker ──────────────────────────────────────────────────

class ProgressTracker:
    """Throttled progress updates for Telegram messages."""

    def __init__(self, total: float, update_interval: float = Config.PROGRESS_UPDATE_INTERVAL):
        self.total = total
        self.current: float = 0.0
        self.start_time: float = time.time()
        self.last_update_time: float = 0.0
        self.update_interval = update_interval

    def update(self, current: float) -> bool:
        """Update progress and return True if enough time has passed for a message update."""
        self.current = current
        now = time.time()
        if now - self.last_update_time >= self.update_interval:
            self.last_update_time = now
            return True
        return False

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, (self.current / self.total) * 100)

    @property
    def eta_seconds(self) -> float:
        elapsed = time.time() - self.start_time
        if self.current <= 0 or elapsed <= 0:
            return 0.0
        rate = self.current / elapsed
        remaining = self.total - self.current
        return remaining / rate if rate > 0 else 0.0

    @property
    def speed(self) -> str:
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return "0 B/s"
        rate = self.current / elapsed
        return f"{human_size(int(rate))}/s"

    def format_progress(self, task_type: str = "Processing") -> str:
        """Generate formatted progress message."""
        bar = progress_bar(self.percent)
        eta = format_eta(self.eta_seconds)
        return (
            f"⚙️ **{task_type}**\n\n"
            f"{bar}\n"
            f"📊 Progress: `{self.percent:.1f}%`\n"
            f"⏱ ETA: `{eta}`\n"
            f"🚀 Speed: `{self.speed}`"
        )


# ── GPU Detection ─────────────────────────────────────────────────────

async def detect_nvidia_gpu() -> Optional[str]:
    """Detect NVIDIA GPU. Returns GPU name or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            gpu_name = stdout.decode().strip().split("\n")[0]
            logger.info("Detected NVIDIA GPU: %s", gpu_name)
            return gpu_name
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("GPU detection error: %s", e)
    return None


async def get_gpu_count() -> int:
    """Get the number of available NVIDIA GPUs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name", "--format=csv,noheader",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            lines = [line.strip() for line in stdout.decode().strip().split("\n") if line.strip()]
            return len(lines)
    except Exception:
        pass
    return 1


async def check_nvenc_support() -> bool:
    """Check if FFmpeg has HEVC NVENC support."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        return "hevc_nvenc" in output
    except Exception:
        return False


async def check_av1_nvenc_support() -> bool:
    """Check if FFmpeg has AV1 NVENC support (RTX 40xx+ GPUs)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        return "av1_nvenc" in output
    except Exception:
        return False


async def verify_gpu_encoding(test_cmd: list[str]) -> bool:
    """Run a quick GPU encode test to verify NVENC actually works.
    
    Call during startup to confirm GPU isn't just detected but functional.
    """
    try:
        # Generate a small test: 1 frame at 256x256 (above NVENC minimum of 129x33)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
            "-c:v", "hevc_nvenc", "-preset", "p1",
            "-f", "null", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("✅ GPU encode test PASSED — NVENC is functional")
            return True
        else:
            logger.warning("❌ GPU encode test FAILED: %s", stderr.decode()[:200])
            return False
    except Exception as e:
        logger.warning("❌ GPU encode test error: %s", e)
        return False
