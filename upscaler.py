"""
AI upscaling engine for AnimeEncoderBot.
Uses Real-ESRGAN with anime-optimized models.
Pipeline: extract frames → upscale → reassemble with audio.
"""

import asyncio
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from config import Config
from utils import get_output_path

logger = logging.getLogger(__name__)

# ── Resolution Targets ────────────────────────────────────────────────

RESOLUTION_MAP: dict[str, tuple[int, int]] = {
    "1080p": (1920, 1080),
    "2k":    (2560, 1440),
    "4k":    (3840, 2160),
    "8k":    (7680, 4320),
}

# Real-ESRGAN scale factors supported
SUPPORTED_SCALES = [2, 3, 4]

# Model names
ANIME_MODEL = "realesr-animevideov3"


class Upscaler:
    """Real-ESRGAN based anime video upscaler."""

    def __init__(self) -> None:
        self._binary: str = Config.REALESRGAN_PATH
        self._available: Optional[bool] = None

    async def check_available(self) -> bool:
        """Check if Real-ESRGAN binary is available."""
        if self._available is not None:
            return self._available
        self._available = shutil.which(self._binary) is not None
        if not self._available:
            # Try alternative paths
            for alt in ["/usr/local/bin/realesrgan-ncnn-vulkan", "./realesrgan-ncnn-vulkan"]:
                if Path(alt).exists():
                    self._binary = alt
                    self._available = True
                    break
        logger.info("Real-ESRGAN available: %s (path: %s)", self._available, self._binary)
        return self._available

    @staticmethod
    def calculate_scale_factor(
        input_width: int,
        input_height: int,
        target_resolution: str,
    ) -> int:
        """Calculate the appropriate scale factor for the target resolution.

        Returns the smallest supported scale that achieves the target.
        """
        target_w, target_h = RESOLUTION_MAP.get(target_resolution, (3840, 2160))

        # Calculate required scale
        scale_w = target_w / input_width
        scale_h = target_h / input_height
        required_scale = max(scale_w, scale_h)

        # Round up to nearest supported scale
        for s in SUPPORTED_SCALES:
            if s >= required_scale:
                return s

        # If even 4x isn't enough, use 4x (user gets what they can)
        return 4

    async def upscale(
        self,
        input_path: str,
        target_resolution: str = "4k",
        progress_callback: Optional[Callable[[int, int], None]] = None,
        gpu_id: int = 0,
    ) -> str:
        """Upscale a video using Real-ESRGAN segment-by-segment pipeline to save disk and prevent hangs.

        Args:
            input_path: Path to input video.
            target_resolution: Target resolution key (1080p, 2k, 4k, 8k).
            progress_callback: Called with (current_frame, total_frames).
            gpu_id: GPU ID to run upscaling on.

        Returns:
            Path to upscaled output video.

        Raises:
            RuntimeError: If upscaling fails.
        """
        if not await self.check_available():
            raise RuntimeError(
                "Real-ESRGAN is not installed. "
                "Install with: apt install realesrgan-ncnn-vulkan or download from "
                "https://github.com/xinntao/Real-ESRGAN/releases"
            )

        work_dir = Path(input_path).parent / f"upscale_{Path(input_path).stem}"
        segments_dir = work_dir / "segments"

        try:
            segments_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Get input video info
            input_info = await self._get_video_info(input_path)
            fps = input_info["fps"]
            width = input_info["width"]
            height = input_info["height"]
            duration = await self._get_video_duration(input_path)

            logger.info(
                "Input: %dx%d @ %s fps, duration: %s s",
                width, height, fps, duration,
            )

            scale = self.calculate_scale_factor(width, height, target_resolution)
            logger.info("Using scale factor: %dx for target %s", scale, target_resolution)

            # Step 2: Segment the input video into 30-second parts
            logger.info("Segmenting video into chunks...")
            segment_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-i", input_path,
                "-c", "copy",
                "-map", "0",
                "-segment_time", "30",
                "-f", "segment",
                "-reset_timestamps", "1",
                str(segments_dir / "part_%03d.mkv"),
            ]
            proc = await asyncio.create_subprocess_exec(
                *segment_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Video segmentation failed: {stderr.decode()}")

            segment_files = sorted(segments_dir.glob("part_*.mkv"))
            if not segment_files:
                raise RuntimeError("No segments were created")

            # Estimate total frames
            total_frames = int(duration * fps) if duration > 0 else 0
            processed_frames = 0
            upscaled_segments = []

            # Step 3: Process each segment
            for i, segment_file in enumerate(segment_files):
                logger.info(f"Processing segment {i+1}/{len(segment_files)}: {segment_file.name}")

                part_work_dir = work_dir / f"part_{i:03d}"
                part_frames_dir = part_work_dir / "frames"
                part_upscaled_dir = part_work_dir / "upscaled"
                part_output_path = part_work_dir / f"upscaled_{segment_file.name}"

                part_frames_dir.mkdir(parents=True, exist_ok=True)
                part_upscaled_dir.mkdir(parents=True, exist_ok=True)

                try:
                    # Extract frames for this segment (JPEG)
                    segment_frames = await self._extract_frames(str(segment_file), part_frames_dir, fps)
                    if segment_frames == 0:
                        logger.warning(f"Segment {segment_file.name} had 0 frames, skipping")
                        continue

                    # If total_frames is 0, estimate it on the fly
                    if total_frames == 0:
                        total_frames = segment_frames * len(segment_files)

                    # Custom progress callback for this segment
                    def segment_progress(done_in_segment: int, total_in_segment: int):
                        if progress_callback:
                            progress_callback(processed_frames + done_in_segment, total_frames)

                    # Upscale frames
                    await self._upscale_frames(
                        part_frames_dir,
                        part_upscaled_dir,
                        scale=scale,
                        progress_callback=segment_progress,
                        total_frames=segment_frames,
                        gpu_id=gpu_id,
                    )

                    # Reassemble segment
                    target_w, target_h = RESOLUTION_MAP.get(target_resolution, (3840, 2160))
                    await self._reassemble_video(
                        part_upscaled_dir,
                        str(segment_file),
                        str(part_output_path),
                        fps,
                        target_w,
                        target_h,
                        gpu_id=gpu_id,
                    )

                    if part_output_path.exists():
                        upscaled_segments.append(part_output_path)
                        processed_frames += segment_frames
                    else:
                        raise RuntimeError(f"Segment reassembly failed for {segment_file.name}")

                finally:
                    # Clean up directories for this segment immediately to reclaim space!
                    if part_frames_dir.exists():
                        shutil.rmtree(part_frames_dir, ignore_errors=True)
                    if part_upscaled_dir.exists():
                        shutil.rmtree(part_upscaled_dir, ignore_errors=True)

            # Step 4: Concatenate all upscaled segments
            if not upscaled_segments:
                raise RuntimeError("No segments were successfully upscaled")

            logger.info("Concatenating upscaled segments...")
            concat_txt_path = work_dir / "concat.txt"
            with open(concat_txt_path, "w", encoding="utf-8") as f:
                for seg in upscaled_segments:
                    # Escape single quotes in path
                    escaped_path = str(seg).replace("'", "'\\''")
                    f.write(f"file '{escaped_path}'\n")

            output_path = get_output_path(input_path, f"upscaled_{target_resolution}", ".mkv")

            concat_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_txt_path),
                "-c", "copy",
                output_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *concat_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Video concatenation failed: {stderr.decode()}")

            if not Path(output_path).exists():
                raise RuntimeError("Concatenation completed but output file not found")

            output_size = Path(output_path).stat().st_size
            logger.info("Upscaling complete: %s (%d bytes)", output_path, output_size)
            return output_path

        finally:
            # Clean up working directory
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
                logger.debug("Cleaned up work dir: %s", work_dir)

    async def _get_video_info(self, file_path: str) -> dict:
        """Get basic video info (width, height, fps)."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "csv=p=0",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        parts = stdout.decode().strip().split(",")

        if len(parts) < 3:
            raise RuntimeError("Could not determine video properties")

        width = int(parts[0])
        height = int(parts[1])

        # Parse fps fraction
        fps_str = parts[2]
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = round(float(num) / float(den), 3)
        else:
            fps = float(fps_str)

        return {"width": width, "height": height, "fps": fps, "fps_str": fps_str}

    async def _get_video_duration(self, file_path: str) -> float:
        """Get video duration in seconds."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            return float(stdout.decode().strip())
        except Exception:
            return 0.0

    async def _extract_frames(self, input_path: str, frames_dir: Path, fps: float) -> int:
        """Extract all frames from video as high-quality JPEG images."""
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", input_path,
            "-q:v", "2",
            "-vsync", "0",
            str(frames_dir / "frame_%08d.jpg"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Frame extraction failed: {stderr.decode()}")

        # Count extracted frames
        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        return len(frame_files)

    async def _upscale_frames(
        self,
        input_dir: Path,
        output_dir: Path,
        scale: int = 4,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_frames: int = 0,
        gpu_id: int = 0,
    ) -> None:
        """Run Real-ESRGAN on extracted frames."""
        cmd = [
            self._binary,
            "-i", str(input_dir),
            "-o", str(output_dir),
            "-n", ANIME_MODEL,
            "-s", str(scale),
            "-f", "png",
            "-g", str(gpu_id),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Monitor output directory for progress
        if progress_callback and total_frames > 0:
            while proc.returncode is None:
                await asyncio.sleep(2)
                done = len(list(output_dir.glob("*.png")))
                try:
                    progress_callback(done, total_frames)
                except Exception:
                    pass
                # Check if process finished
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
        else:
            await proc.wait()

        stdout_data = await proc.stdout.read() if proc.stdout else b""
        stderr_data = await proc.stderr.read() if proc.stderr else b""

        if proc.returncode != 0:
            error_msg = stderr_data.decode("utf-8", errors="replace")
            raise RuntimeError(f"Real-ESRGAN failed (exit {proc.returncode}): {error_msg}")

        # Verify frames were upscaled
        upscaled_count = len(list(output_dir.glob("*.png")))
        if upscaled_count == 0:
            raise RuntimeError("Real-ESRGAN produced no output frames")

        logger.info("Upscaled %d frames", upscaled_count)

    async def _reassemble_video(
        self,
        frames_dir: Path,
        original_input: str,
        output_path: str,
        fps: float,
        target_w: int,
        target_h: int,
        gpu_id: int = 0,
    ) -> None:
        """Reassemble upscaled frames into video with original audio.
        Uses GPU (NVENC) for encoding when available.
        """
        from utils import detect_nvidia_gpu, check_nvenc_support

        gpu_name = await detect_nvidia_gpu()
        has_nvenc = await check_nvenc_support() if gpu_name else False

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            # Input upscaled frames
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%08d.png"),
            # Input original for audio/subs
            "-i", original_input,
            # Scale to exact target (in case upscale overshot)
            "-vf", f"scale={target_w}:{target_h}:flags=lanczos",
        ]

        if has_nvenc:
            # GPU-accelerated encoding for reassembly
            logger.info(f"Reassembling with HEVC NVENC (GPU {gpu_id})")
            cmd.extend([
                "-c:v", "hevc_nvenc",
                "-preset", "p5",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "18",
                "-b:v", "0",
                "-maxrate", "30M",
                "-bufsize", "60M",
                "-spatial_aq", "1",
                "-temporal_aq", "1",
                "-rc-lookahead", "32",
                "-profile:v", "main10",
            ])
            if gpu_id is not None:
                cmd.extend(["-gpu", str(gpu_id)])
        else:
            # CPU fallback
            logger.warning("Reassembling with libx265 (CPU) — no GPU available")
            cmd.extend([
                "-c:v", "libx265",
                "-crf", "16",
                "-preset", "medium",
                "-pix_fmt", "yuv420p10le",
            ])

        cmd.extend([
            # Copy audio from original
            "-c:a", "copy",
            # Copy subtitles
            "-c:s", "copy",
            # Map streams
            "-map", "0:v:0",   # Video from upscaled frames
            "-map", "1:a?",    # Audio from original
            "-map", "1:s?",    # Subtitles from original
            output_path,
        ])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            # If NVENC failed, retry with CPU
            if has_nvenc:
                logger.warning("NVENC reassembly failed, retrying with CPU...")
                cmd_cpu = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                    "-framerate", str(fps),
                    "-i", str(frames_dir / "frame_%08d.png"),
                    "-i", original_input,
                    "-vf", f"scale={target_w}:{target_h}:flags=lanczos",
                    "-c:v", "libx265", "-crf", "16", "-preset", "medium",
                    "-pix_fmt", "yuv420p10le",
                    "-c:a", "copy", "-c:s", "copy",
                    "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
                    output_path,
                ]
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_cpu,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr2 = await proc2.communicate()
                if proc2.returncode != 0:
                    raise RuntimeError(f"Video reassembly failed: {stderr2.decode()}")
            else:
                raise RuntimeError(f"Video reassembly failed: {stderr.decode()}")


# Global upscaler instance
upscaler = Upscaler()
