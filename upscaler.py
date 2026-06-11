"""
AI upscaling engine for AnimeEncoderBot.
Uses Real-CUGAN (Bilibili) with anime-optimized models.
Pipeline: extract frames → upscale → reassemble with audio.
"""

import asyncio
import concurrent.futures
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

# Real-CUGAN natively supports 2x, 3x, 4x
SUPPORTED_SCALES = [2, 3, 4]

# Real-CUGAN weights are bundled in the repo's weights/ directory


class Upscaler:
    """Real-CUGAN based anime video upscaler (PyTorch/CUDA).

    Uses Bilibili's Real-CUGAN model which natively supports 2x/3x/4x
    upscaling with anime-optimized weights.
    """

    def __init__(self) -> None:
        self._available: Optional[bool] = None
        self._backend: Optional[str] = None  # "pytorch" | None

    @property
    def backend(self) -> Optional[str]:
        return self._backend

    async def check_available(self) -> bool:
        """Check if Real-CUGAN (PyTorch/CUDA) is usable."""
        if self._available is not None:
            return self._available

        try:
            import torch
            if torch.cuda.is_available():
                self._backend = "pytorch"
                self._available = True
                logger.info(
                    "Real-CUGAN backend: pytorch (CUDA devices: %d)", torch.cuda.device_count()
                )
                return True
            else:
                logger.info("PyTorch available but CUDA not accessible")
        except ImportError:
            logger.info("PyTorch not installed")

        self._available = False
        logger.warning("Real-CUGAN not available (no CUDA)")
        return False

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
        """Upscale a video using a multi-GPU Real-CUGAN segment pipeline.

        The old pipeline extracted and upscaled one 30-second segment at a time.
        On Kaggle T4 x2 that leaves one GPU idle and makes Telegram sit on
        "Extracting video frames..." for a long time. This version uses smaller
        segments, reports extraction progress, and processes several segments in
        parallel across the configured GPU IDs.
        """
        if not await self.check_available():
            raise RuntimeError(
                "Real-CUGAN is not available. "
                "Requires PyTorch with CUDA support."
            )

        work_dir = Path(input_path).parent / f"upscale_{Path(input_path).stem}"
        segments_dir = work_dir / "segments"

        try:
            segments_dir.mkdir(parents=True, exist_ok=True)

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
            target_w, target_h = RESOLUTION_MAP.get(target_resolution, (3840, 2160))
            total_frames = max(1, int(duration * fps)) if duration > 0 and fps > 0 else 0
            segment_seconds = max(2, Config.UPSCALE_SEGMENT_SECONDS)
            gpu_ids = await self._resolve_gpu_ids(gpu_id)
            parallel_jobs = self._resolve_parallel_jobs(gpu_ids, expected_segments=math.ceil(duration / segment_seconds) if duration else 1)

            logger.info(
                "Using scale %dx for %s; segment=%ss; GPUs=%s; parallel_jobs=%s; Real-CUGAN threads=%s; output=%s",
                scale,
                target_resolution,
                segment_seconds,
                ",".join(map(str, gpu_ids)),
                parallel_jobs,
                Config.REALESRGAN_THREADS,
                Config.REALESRGAN_OUTPUT_FORMAT,
            )

            # Smaller chunks let Real-CUGAN start quickly and allow multiple GPUs
            # to work at the same time instead of waiting on one huge extraction.
            logger.info("Segmenting video into %s-second chunks...", segment_seconds)
            segment_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-i", input_path,
                "-c", "copy",
                "-map", "0",
                "-segment_time", str(segment_seconds),
                "-f", "segment",
                "-reset_timestamps", "1",
                str(segments_dir / "part_%03d.mkv"),
            ]
            _, stderr = await self._run_process(segment_cmd)
            if stderr:
                logger.debug("Segmentation stderr: %s", stderr[:500])

            segment_files = sorted(segments_dir.glob("part_*.mkv"))
            if not segment_files:
                raise RuntimeError("No segments were created")

            if total_frames == 0:
                total_frames = await self._estimate_total_frames(segment_files)

            total_work_units = max(1, total_frames * 2)
            extract_progress: list[int] = [0] * len(segment_files)
            upscale_progress: list[int] = [0] * len(segment_files)
            progress_lock = asyncio.Lock()

            def fire_progress(index: int, phase: str, done_in_segment: int, segment_frames: int) -> None:
                """Synchronous progress update — avoids unawaited coroutine warnings."""
                if not progress_callback or total_frames <= 0:
                    return
                if phase == "extract":
                    extract_progress[index] = min(segment_frames, max(extract_progress[index], done_in_segment))
                else:
                    extract_progress[index] = max(extract_progress[index], segment_frames)
                    upscale_progress[index] = min(segment_frames, max(upscale_progress[index], done_in_segment))
                done = min(sum(extract_progress) + sum(upscale_progress), total_work_units)
                try:
                    progress_callback(done, total_work_units)
                except Exception:
                    pass

            # ── Queue-based GPU workers for true parallelism ──────────────────
            # Each GPU gets its own worker that continuously pulls segments from
            # a shared queue. This ensures GPUs don't wait for each other.
            segment_queue: asyncio.Queue[tuple[int, Path]] = asyncio.Queue()
            for i, seg in enumerate(segment_files):
                await segment_queue.put((i, seg))

            results: list[tuple[int, Path, int]] = []
            results_lock = asyncio.Lock()
            reassembly_tasks: list[asyncio.Task] = []

            async def gpu_worker(worker_gpu: int) -> None:
                """Worker loop: pull segments from queue and process on assigned GPU."""
                while True:
                    try:
                        index, segment_file = segment_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return  # No more work

                    logger.info(
                        "Processing segment %d/%d on GPU %s: %s",
                        index + 1,
                        len(segment_files),
                        worker_gpu,
                        segment_file.name,
                    )

                    part_work_dir = work_dir / f"part_{index:03d}"
                    part_frames_dir = part_work_dir / "frames"
                    part_upscaled_dir = part_work_dir / "upscaled"
                    part_output_path = part_work_dir / f"upscaled_{segment_file.name}"

                    part_frames_dir.mkdir(parents=True, exist_ok=True)
                    part_upscaled_dir.mkdir(parents=True, exist_ok=True)

                    try:
                        expected_segment_frames = await self._estimate_segment_frames(
                            str(segment_file), fps, segment_seconds
                        )

                        extracted_frames = await self._extract_frames(
                            str(segment_file),
                            part_frames_dir,
                            expected_frames=expected_segment_frames,
                            progress_callback=lambda done, _total, idx=index, exp=expected_segment_frames: fire_progress(idx, "extract", done, exp),
                        )
                        if extracted_frames == 0:
                            raise RuntimeError(f"Segment {segment_file.name} had 0 frames")

                        await self._upscale_frames(
                            part_frames_dir,
                            part_upscaled_dir,
                            scale=scale,
                            progress_callback=lambda done, _total, idx=index, ef=extracted_frames: fire_progress(idx, "upscale", done, ef),
                            total_frames=extracted_frames,
                            gpu_id=worker_gpu,
                        )

                        # Run reassembly in background so GPU can start next segment
                        async def do_reassembly(
                            idx: int,
                            upscaled_dir: Path,
                            seg_file: Path,
                            out_path: Path,
                            frames_dir: Path,
                            upscaled_frames_dir: Path,
                            n_frames: int,
                        ):
                            try:
                                await self._reassemble_video(
                                    upscaled_dir,
                                    str(seg_file),
                                    str(out_path),
                                    fps,
                                    target_w,
                                    target_h,
                                    gpu_id=worker_gpu,
                                    frame_ext=Config.REALESRGAN_OUTPUT_FORMAT,
                                )

                                if not out_path.exists():
                                    raise RuntimeError(f"Segment reassembly failed for {seg_file.name}")

                                # Delete input segment NOW (after reassembly grabbed audio)
                                try:
                                    seg_file.unlink()
                                except OSError:
                                    pass

                                fire_progress(idx, "upscale", n_frames, n_frames)

                                async with results_lock:
                                    results.append((idx, out_path, n_frames))
                            finally:
                                shutil.rmtree(frames_dir, ignore_errors=True)
                                shutil.rmtree(upscaled_frames_dir, ignore_errors=True)

                        # Start reassembly in background, don't wait
                        task = asyncio.create_task(do_reassembly(
                            index,
                            part_upscaled_dir,
                            segment_file,
                            part_output_path,
                            part_frames_dir,
                            part_upscaled_dir,
                            extracted_frames,
                        ))
                        reassembly_tasks.append(task)

                    except Exception:
                        # Cleanup on error
                        shutil.rmtree(part_frames_dir, ignore_errors=True)
                        shutil.rmtree(part_upscaled_dir, ignore_errors=True)
                        raise

            # Start one worker per GPU — they run truly in parallel
            await asyncio.gather(*(gpu_worker(g) for g in gpu_ids))
            
            # Wait for any remaining reassembly tasks
            if reassembly_tasks:
                await asyncio.gather(*reassembly_tasks, return_exceptions=True)
            
            results.sort(key=lambda item: item[0])
            upscaled_segments = [path for _, path, _ in results]

            if not upscaled_segments:
                raise RuntimeError("No segments were successfully upscaled")

            logger.info("Concatenating %d upscaled segments...", len(upscaled_segments))
            concat_txt_path = work_dir / "concat.txt"
            with open(concat_txt_path, "w", encoding="utf-8") as f:
                for seg in upscaled_segments:
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
            _, stderr = await self._run_process(concat_cmd)
            if stderr:
                logger.debug("Concatenation stderr: %s", stderr[:500])

            if not Path(output_path).exists():
                raise RuntimeError("Concatenation completed but output file not found")

            if progress_callback and total_frames > 0:
                progress_callback(total_work_units, total_work_units)

            output_size = Path(output_path).stat().st_size
            logger.info("Upscaling complete: %s (%d bytes)", output_path, output_size)
            return output_path

        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
                logger.debug("Cleaned up work dir: %s", work_dir)

    async def _resolve_gpu_ids(self, fallback_gpu_id: int) -> list[int]:
        """Resolve available CUDA GPU IDs."""
        configured = Config.REALESRGAN_GPU_IDS.strip().lower()
        if configured and configured != "auto":
            gpu_ids = [int(item.strip()) for item in configured.split(",") if item.strip().isdigit()]
            if gpu_ids:
                return gpu_ids

        import torch
        count = torch.cuda.device_count()
        if count > 0:
            gpu_ids = list(range(count))
            logger.info("PyTorch CUDA devices: %s", gpu_ids)
            return gpu_ids
        logger.warning("No CUDA devices, falling back to GPU %d", fallback_gpu_id)
        return [fallback_gpu_id]

    @staticmethod
    def _resolve_parallel_jobs(gpu_ids: list[int], expected_segments: int) -> int:
        """Choose how many Real-CUGAN segment workers to run at once."""
        configured_jobs = Config.UPSCALE_PARALLEL_JOBS
        if configured_jobs > 0:
            return max(1, min(configured_jobs, expected_segments))

        jobs_per_gpu = max(1, Config.UPSCALE_JOBS_PER_GPU)
        auto_jobs = max(1, len(gpu_ids) * jobs_per_gpu)
        return max(1, min(auto_jobs, expected_segments))

    @staticmethod
    async def _run_process(cmd: list[str]) -> tuple[str, str]:
        """Run a subprocess and return stdout/stderr, raising on failure."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr_text}")
        return stdout_text, stderr_text

    async def _estimate_total_frames(self, segment_files: list[Path]) -> int:
        """Estimate total frame count when container duration is unavailable."""
        total = 0
        for segment_file in segment_files:
            info = await self._get_video_info(str(segment_file))
            duration = await self._get_video_duration(str(segment_file))
            total += max(1, int(duration * info["fps"])) if duration > 0 else 0
        return max(1, total)

    async def _estimate_segment_frames(self, segment_file: str, fps: float, segment_seconds: int) -> int:
        """Estimate frame count for progress during frame extraction."""
        duration = await self._get_video_duration(segment_file)
        if duration > 0 and fps > 0:
            return max(1, int(duration * fps))
        if fps > 0:
            return max(1, int(segment_seconds * fps))
        return 0

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

    async def _extract_frames(
        self,
        input_path: str,
        frames_dir: Path,
        expected_frames: int = 0,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Extract frames from a segment and report progress while ffmpeg runs."""
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", input_path,
            "-pix_fmt", "yuvj420p",
            "-q:v", "2",
            "-vsync", "vfr",
            str(frames_dir / "frame_%08d.jpg"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communicate_task = asyncio.create_task(proc.communicate())

        while not communicate_task.done():
            await asyncio.sleep(1)
            if progress_callback and expected_frames > 0:
                done = len(list(frames_dir.glob("frame_*.jpg")))
                try:
                    progress_callback(done, expected_frames)
                except Exception:
                    pass

        stdout, stderr = await communicate_task
        if proc.returncode != 0:
            raise RuntimeError(f"Frame extraction failed: {stderr.decode('utf-8', errors='replace')}")

        frame_count = len(list(frames_dir.glob("frame_*.jpg")))
        if progress_callback and expected_frames > 0:
            progress_callback(frame_count, expected_frames)
        return frame_count

    async def _ensure_cugan_weights(self, scale: int) -> Path:
        """Resolve Real-CUGAN weight file for the given scale.

        Weights are bundled in the repo's weights/ directory.
        """
        weight_name = f"up{scale}x-latest-denoise3x.pth"
        # Look in the bot's own weights/ directory (bundled in repo)
        bot_dir = Path(__file__).parent
        model_path = bot_dir / "weights" / weight_name

        if model_path.exists():
            return model_path

        # Fallback: check ~/.cache/realcugan
        cache_path = Path.home() / ".cache" / "realcugan" / weight_name
        if cache_path.exists():
            return cache_path

        raise FileNotFoundError(
            f"Real-CUGAN {scale}x weights not found at {model_path} or {cache_path}. "
            f"Download from Google Drive and place in weights/ directory."
        )

    async def _upscale_frames(
        self,
        input_dir: Path,
        output_dir: Path,
        scale: int = 4,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_frames: int = 0,
        gpu_id: int = 0,
    ) -> None:
        """Upscale frames using Real-CUGAN (PyTorch/CUDA).

        The heavy inference loop is offloaded to a thread so it doesn't
        block the asyncio event loop — critical for true parallel GPU
        utilisation when multiple segments are processed via gather().
        """
        import cv2
        import numpy as np
        import torch

        output_format = Config.REALESRGAN_OUTPUT_FORMAT if Config.REALESRGAN_OUTPUT_FORMAT in {"jpg", "png", "webp"} else "jpg"
        model_path = await self._ensure_cugan_weights(scale)

        def _blocking_upscale() -> int:
            import sys
            bot_dir = str(Path(__file__).parent)
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            from upcunet_v3 import RealWaifuUpScaler

            torch.cuda.set_device(gpu_id)
            device = torch.device(f"cuda:{gpu_id}")
            logger.info("Real-CUGAN: GPU %d, scale %dx", gpu_id, scale)

            model = RealWaifuUpScaler(
                scale=scale, weight_path=str(model_path),
                half=True, device=device,
            )
            tile_mode = 0 if Config.REALESRGAN_TILE_SIZE <= 0 else 3

            frame_files = sorted(input_dir.glob("frame_*.jpg"))
            upscaled_count = 0

            for frame_file in frame_files:
                img = cv2.imread(str(frame_file), cv2.IMREAD_UNCHANGED)
                if img is None:
                    logger.warning("Could not read frame: %s", frame_file)
                    continue

                result = model(img, tile_mode=tile_mode, cache_mode=0, alpha=1)
                # Real-CUGAN already returns uint8 (0-255), no scaling needed
                if result.dtype != np.uint8:
                    result = np.clip(result * 255, 0, 255).astype(np.uint8)

                out_name = frame_file.stem + f".{output_format}"
                cv2.imwrite(str(output_dir / out_name), result)
                upscaled_count += 1

                if progress_callback and total_frames > 0:
                    try:
                        progress_callback(upscaled_count, total_frames)
                    except Exception:
                        pass

            del model
            torch.cuda.empty_cache()
            return upscaled_count

        upscaled_count = await asyncio.get_event_loop().run_in_executor(None, _blocking_upscale)

        if upscaled_count == 0:
            raise RuntimeError("Real-CUGAN produced no output frames")

        if progress_callback and total_frames > 0:
            progress_callback(upscaled_count, total_frames)
        logger.info("Upscaled %d frames on GPU %d (Real-CUGAN)", upscaled_count, gpu_id)

    async def _reassemble_video(
        self,
        frames_dir: Path,
        original_input: str,
        output_path: str,
        fps: float,
        target_w: int,
        target_h: int,
        gpu_id: int = 0,
        frame_ext: str = "jpg",
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
            "-i", str(frames_dir / f"frame_%08d.{frame_ext}"),
            # Input original for audio/subs
            "-i", original_input,
            # Scale to exact target (in case upscale overshot)
            "-vf", f"scale={target_w}:{target_h}:flags=lanczos",
        ]

        # Use near-lossless H.264 intermediate so encoder.py controls final quality.
        # -crf 12 balances quality vs disk space for intermediates (final encode sets quality).
        logger.info(f"Reassembling with near-lossless H.264 intermediate (GPU {gpu_id})")
        cmd.extend([
            "-c:v", "libx264",
            "-crf", "12",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
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
                    "-i", str(frames_dir / f"frame_%08d.{frame_ext}"),
                    "-i", original_input,
                    "-vf", f"scale={target_w}:{target_h}:flags=lanczos",
                    "-c:v", "libx264", "-crf", "12", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",
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
