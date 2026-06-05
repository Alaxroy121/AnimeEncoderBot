"""
FFmpeg encoding engine for AnimeEncoderBot.
Supports AV1 (SVT-AV1) and H.265/HEVC (NVENC + CPU fallback).
Optimized for NVIDIA T4 and other NVIDIA GPUs.
"""

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils import detect_nvidia_gpu, check_nvenc_support, get_output_path

logger = logging.getLogger(__name__)

# ── Quality / Preset Maps ────────────────────────────────────────────

# CRF/CQ values per quality tier (lower = better quality, larger file)
QUALITY_MAP = {
    "low":    {"hevc_crf": 30, "hevc_cq": 32, "av1_crf": 38, "av1_cq": 38},
    "medium": {"hevc_crf": 24, "hevc_cq": 26, "av1_crf": 30, "av1_cq": 30},
    "high":   {"hevc_crf": 20, "hevc_cq": 22, "av1_crf": 24, "av1_cq": 24},
    "ultra":  {"hevc_crf": 16, "hevc_cq": 18, "av1_crf": 18, "av1_cq": 18},
}

# FFmpeg preset names per codec
HEVC_NVENC_PRESETS = {
    "fast": "p4",        # NVENC P4 — fast
    "medium": "p5",      # NVENC P5 — balanced
    "slow": "p6",        # NVENC P6 — quality
    "veryslow": "p7",    # NVENC P7 — max quality
}

HEVC_CPU_PRESETS = {
    "fast": "fast",
    "medium": "medium",
    "slow": "slow",
    "veryslow": "veryslow",
}

SVT_AV1_PRESETS = {
    "fast": "8",         # SVT-AV1 preset 8 — fast
    "medium": "6",       # SVT-AV1 preset 6 — balanced
    "slow": "4",         # SVT-AV1 preset 4 — quality
    "veryslow": "2",     # SVT-AV1 preset 2 — max quality (very slow)
}


@dataclass
class EncodeSettings:
    """Encoding parameters."""
    codec: str = "hevc"             # hevc or av1
    quality: str = "medium"         # low, medium, high, ultra
    preset: str = "medium"          # fast, medium, slow, veryslow
    audio_codec: str = "copy"       # copy, aac, opus
    audio_bitrate: str = "192k"
    subtitle_mode: str = "copy"     # copy, burn, none
    use_gpu: bool = True
    resolution: Optional[str] = None  # None = keep original, or "1920x1080" etc.
    extra_flags: list[str] = field(default_factory=list)


class Encoder:
    """FFmpeg encoding engine with GPU acceleration."""

    def __init__(self) -> None:
        self._gpu_name: Optional[str] = None
        self._has_nvenc: bool = False
        self._initialized: bool = False

    async def initialize(self) -> None:
        """Detect GPU and NVENC capabilities."""
        if self._initialized:
            return
        self._gpu_name = await detect_nvidia_gpu()
        if self._gpu_name:
            self._has_nvenc = await check_nvenc_support()
            logger.info(
                "GPU: %s | NVENC: %s",
                self._gpu_name,
                "available" if self._has_nvenc else "not available",
            )
        else:
            logger.info("No NVIDIA GPU detected — using CPU encoding")
        self._initialized = True

    @property
    def gpu_available(self) -> bool:
        return self._has_nvenc and self._gpu_name is not None

    @property
    def gpu_name(self) -> str:
        return self._gpu_name or "None"

    def _build_command(
        self,
        input_path: str,
        output_path: str,
        settings: EncodeSettings,
    ) -> list[str]:
        """Build the full FFmpeg command."""
        cmd: list[str] = ["ffmpeg", "-y", "-hide_banner"]

        # Hardware decode if using GPU
        use_gpu = settings.use_gpu and self.gpu_available
        if use_gpu:
            cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

        cmd.extend(["-i", input_path])

        # ── Video Codec ──
        if settings.codec == "hevc":
            if use_gpu:
                cmd.extend(self._hevc_nvenc_args(settings))
            else:
                cmd.extend(self._hevc_cpu_args(settings))
        elif settings.codec == "av1":
            # SVT-AV1 is always CPU — it's fast and efficient
            if use_gpu:
                # Download from GPU first if hwaccel was used
                cmd.extend(["-hwaccel_output_format", "nv12"])
            cmd.extend(self._svt_av1_args(settings))

        # ── Resolution scaling ──
        if settings.resolution:
            w, h = settings.resolution.split("x")
            if use_gpu and settings.codec == "hevc":
                cmd.extend(["-vf", f"scale_cuda={w}:{h}:interp_algo=lanczos"])
            else:
                cmd.extend(["-vf", f"scale={w}:{h}:flags=lanczos"])

        # ── Audio ──
        if settings.audio_codec == "copy":
            cmd.extend(["-c:a", "copy"])
        elif settings.audio_codec == "opus":
            cmd.extend(["-c:a", "libopus", "-b:a", settings.audio_bitrate])
        elif settings.audio_codec == "aac":
            cmd.extend(["-c:a", "aac", "-b:a", settings.audio_bitrate])

        # ── Subtitles ──
        if settings.subtitle_mode == "copy":
            cmd.extend(["-c:s", "copy"])
        elif settings.subtitle_mode == "none":
            cmd.extend(["-sn"])
        # "burn" is handled via -vf subtitles filter (complex — needs separate handling)

        # ── Extra flags ──
        cmd.extend(settings.extra_flags)

        # ── Output ──
        cmd.extend(["-map", "0", output_path])
        return cmd

    def _hevc_nvenc_args(self, settings: EncodeSettings) -> list[str]:
        """HEVC NVENC arguments optimized for T4."""
        preset = HEVC_NVENC_PRESETS.get(settings.preset, "p5")
        cq = QUALITY_MAP[settings.quality]["hevc_cq"]
        return [
            "-c:v", "hevc_nvenc",
            "-preset", preset,
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(cq),
            "-b:v", "0",
            "-maxrate", "20M",
            "-bufsize", "40M",
            "-spatial_aq", "1",
            "-temporal_aq", "1",
            "-rc-lookahead", "32",
            "-bf", "4",
            "-b_ref_mode", "middle",
            "-tier", "high",
            "-profile:v", "main10",
            "-pix_fmt", "p010le",
        ]

    def _hevc_cpu_args(self, settings: EncodeSettings) -> list[str]:
        """HEVC CPU (libx265) arguments."""
        preset = HEVC_CPU_PRESETS.get(settings.preset, "medium")
        crf = QUALITY_MAP[settings.quality]["hevc_crf"]
        return [
            "-c:v", "libx265",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p10le",
            "-x265-params",
            "strong-intra-smoothing=0:rect=1:aq-mode=3",
        ]

    def _svt_av1_args(self, settings: EncodeSettings) -> list[str]:
        """SVT-AV1 arguments."""
        preset = SVT_AV1_PRESETS.get(settings.preset, "6")
        crf = QUALITY_MAP[settings.quality]["av1_crf"]
        return [
            "-c:v", "libsvtav1",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p10le",
            "-svtav1-params",
            "tune=0:film-grain=0:enable-overlays=1:scd=1",
            "-g", "240",
        ]

    async def encode(
        self,
        input_path: str,
        settings: EncodeSettings,
        progress_callback: Optional[Callable[[float, float], None]] = None,
    ) -> str:
        """Encode a video file. Returns the output file path.

        Args:
            input_path: Path to input video file.
            settings: Encoding settings.
            progress_callback: Called with (current_seconds, total_seconds).

        Returns:
            Path to the encoded output file.

        Raises:
            RuntimeError: If encoding fails.
        """
        await self.initialize()

        # Determine output extension
        ext = ".mkv" if settings.subtitle_mode == "copy" else ".mp4"
        codec_tag = settings.codec.upper()
        output_path = get_output_path(
            input_path,
            f"{codec_tag}_{settings.quality}",
            ext,
        )

        # Rebuild command if GPU not available but was requested
        if settings.use_gpu and not self.gpu_available:
            logger.warning("GPU requested but not available — falling back to CPU")
            settings.use_gpu = False

        cmd = self._build_command(input_path, output_path, settings)
        logger.info("Encoding command: %s", " ".join(cmd))

        # Get duration for progress tracking
        duration = await self._get_duration(input_path)

        # Run FFmpeg with progress parsing
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Parse FFmpeg stderr for progress
        current_time = 0.0
        stderr_output = []

        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            stderr_output.append(text)

            # Parse time= from FFmpeg output
            match = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", text)
            if match and duration > 0:
                h, m, s, cs = (int(x) for x in match.groups())
                current_time = h * 3600 + m * 60 + s + cs / 100
                if progress_callback:
                    try:
                        progress_callback(current_time, duration)
                    except Exception:
                        pass

        await proc.wait()

        if proc.returncode != 0:
            error_tail = "\n".join(stderr_output[-20:])
            logger.error("Encoding failed (exit %d):\n%s", proc.returncode, error_tail)
            raise RuntimeError(f"FFmpeg encoding failed (exit code {proc.returncode})")

        if not Path(output_path).exists():
            raise RuntimeError("FFmpeg completed but output file not found")

        output_size = Path(output_path).stat().st_size
        logger.info(
            "Encoding complete: %s (%s bytes)",
            output_path,
            output_size,
        )
        return output_path

    async def _get_duration(self, file_path: str) -> float:
        """Get video duration in seconds using ffprobe."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except Exception:
            return 0.0

    @staticmethod
    def get_supported_codecs() -> dict[str, str]:
        """Return dict of supported codec IDs to display names."""
        return {
            "hevc": "H.265/HEVC (NVENC + CPU)",
            "av1": "AV1 (SVT-AV1)",
        }


# Global encoder instance
encoder = Encoder()
