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
    """Real-ESRGAN based anime video upscaler.

    Supports two backends:
      - **ncnn**: realesrgan-ncnn-vulkan binary (requires Vulkan GPU drivers)
      - **pytorch**: realesrgan Python package with CUDA (fallback for Kaggle/CUDA-only)
    """

    def __init__(self) -> None:
        self._binary: str = Config.REALESRGAN_PATH
        self._available: Optional[bool] = None
        self._backend: Optional[str] = None  # "ncnn" | "pytorch" | None

    @property
    def backend(self) -> Optional[str]:
        return self._backend

    async def check_available(self) -> bool:
        """Detect which Real-ESRGAN backend is usable.

        Priority:
          1. ncnn-vulkan binary with real hardware Vulkan GPUs
          2. PyTorch realesrgan package with CUDA
          3. Not available
        """
        if self._available is not None:
            return self._available

        # --- Try ncnn-vulkan binary ---
        binary_found = shutil.which(self._binary) is not None
        if not binary_found:
            for alt in ["/usr/local/bin/realesrgan-ncnn-vulkan", "./realesrgan-ncnn-vulkan"]:
                if Path(alt).exists():
                    self._binary = alt
                    binary_found = True
                    break

        if binary_found:
            hw_gpus = await self._probe_vulkan_gpus()
            if hw_gpus:
                self._backend = "ncnn"
                self._available = True
                logger.info("Real-ESRGAN backend: ncnn-vulkan (binary: %s, GPUs: %s)", self._binary, hw_gpus)
                return True
            else:
                logger.info("ncnn-vulkan binary found but no hardware Vulkan GPUs — skipping ncnn backend")

        # --- Try PyTorch backend ---
        try:
            import torch
            if torch.cuda.is_available():
                # Fix: basicsr uses torchvision.transforms.functional_tensor
                # which was removed in newer torchvision versions
                try:
                    import torchvision.transforms.functional_tensor  # noqa: F401
                except ModuleNotFoundError:
                    import sys
                    import torchvision.transforms.functional as _functional
                    sys.modules["torchvision.transforms.functional_tensor"] = _functional
                    logger.info("Patched torchvision.transforms.functional_tensor compatibility shim")
                import realesrgan  # noqa: F401
                self._backend = "pytorch"
                self._available = True
                logger.info(
                    "Real-ESRGAN backend: pytorch (CUDA devices: %d)", torch.cuda.device_count()
                )
                return True
            else:
                logger.info("PyTorch available but CUDA not accessible — skipping pytorch backend")
        except ImportError:
            logger.info("PyTorch realesrgan package not installed — skipping pytorch backend")

        self._available = False
        logger.warning("Real-ESRGAN not available (no usable backend found)")
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
        """Upscale a video using a multi-GPU Real-ESRGAN segment pipeline.

        The old pipeline extracted and upscaled one 30-second segment at a time.
        On Kaggle T4 x2 that leaves one GPU idle and makes Telegram sit on
        "Extracting video frames..." for a long time. This version uses smaller
        segments, reports extraction progress, and processes several segments in
        parallel across the configured GPU IDs.
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
                "Using scale %dx for %s; segment=%ss; GPUs=%s; parallel_jobs=%s; Real-ESRGAN threads=%s; output=%s",
                scale,
                target_resolution,
                segment_seconds,
                ",".join(map(str, gpu_ids)),
                parallel_jobs,
                Config.REALESRGAN_THREADS,
                Config.REALESRGAN_OUTPUT_FORMAT,
            )

            # Smaller chunks let Real-ESRGAN start quickly and allow multiple GPUs
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

            async def update_progress(index: int, phase: str, done_in_segment: int, segment_frames: int) -> None:
                if not progress_callback or total_frames <= 0:
                    return
                async with progress_lock:
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

            semaphore = asyncio.Semaphore(parallel_jobs)

            async def process_segment(index: int, segment_file: Path) -> tuple[int, Path, int]:
                async with semaphore:
                    assigned_gpu = gpu_ids[index % len(gpu_ids)]
                    logger.info(
                        "Processing segment %d/%d on GPU %s: %s",
                        index + 1,
                        len(segment_files),
                        assigned_gpu,
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
                            progress_callback=lambda done, _total: asyncio.create_task(
                                update_progress(index, "extract", done, expected_segment_frames)
                            ),
                        )
                        if extracted_frames == 0:
                            raise RuntimeError(f"Segment {segment_file.name} had 0 frames")

                        await self._upscale_frames(
                            part_frames_dir,
                            part_upscaled_dir,
                            scale=scale,
                            progress_callback=lambda done, _total: asyncio.create_task(
                                update_progress(index, "upscale", done, extracted_frames)
                            ),
                            total_frames=extracted_frames,
                            gpu_id=assigned_gpu,
                        )

                        await self._reassemble_video(
                            part_upscaled_dir,
                            str(segment_file),
                            str(part_output_path),
                            fps,
                            target_w,
                            target_h,
                            gpu_id=assigned_gpu,
                            frame_ext=Config.REALESRGAN_OUTPUT_FORMAT,
                        )

                        if not part_output_path.exists():
                            raise RuntimeError(f"Segment reassembly failed for {segment_file.name}")

                        await update_progress(index, "upscale", extracted_frames, extracted_frames)
                        return index, part_output_path, extracted_frames

                    finally:
                        shutil.rmtree(part_frames_dir, ignore_errors=True)
                        shutil.rmtree(part_upscaled_dir, ignore_errors=True)

            results = await asyncio.gather(
                *(process_segment(i, segment_file) for i, segment_file in enumerate(segment_files))
            )
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
        """Resolve GPU IDs based on the active backend.

        - ncnn backend: probe Vulkan devices (Real-ESRGAN uses Vulkan, not CUDA)
        - pytorch backend: use ``torch.cuda.device_count()``
        """
        configured = Config.REALESRGAN_GPU_IDS.strip().lower()
        if configured and configured != "auto":
            gpu_ids = [int(item.strip()) for item in configured.split(",") if item.strip().isdigit()]
            if gpu_ids:
                return gpu_ids

        if self._backend == "pytorch":
            import torch
            count = torch.cuda.device_count()
            if count > 0:
                gpu_ids = list(range(count))
                logger.info("PyTorch CUDA devices: %s", gpu_ids)
                return gpu_ids
            logger.warning("PyTorch backend but no CUDA devices, falling back to GPU %d", fallback_gpu_id)
            return [fallback_gpu_id]

        # ncnn backend: probe Vulkan devices
        vulkan_gpu_ids = await self._probe_vulkan_gpus()
        if vulkan_gpu_ids:
            return vulkan_gpu_ids

        logger.warning("Could not detect Vulkan GPUs for Real-ESRGAN, falling back to GPU %d", fallback_gpu_id)
        return [fallback_gpu_id]

    async def _probe_vulkan_gpus(self) -> list[int]:
        """Run Real-ESRGAN with bogus args to list Vulkan devices.

        Parse stderr for device lines like ``[0 NVIDIA Tesla T4]`` and skip
        software renderers (llvmpipe, SwiftShader, lavapipe).
        """
        import re as _re

        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, "-i", ".", "-o", ".", "-n", "realesr-animevideov3",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            output = stderr.decode("utf-8", errors="replace")

            software_renderers = {"llvmpipe", "swiftshader", "lavapipe"}
            gpu_ids: list[int] = []

            for match in _re.finditer(r"\[(\d+)\s+([^\]]+)\]", output):
                dev_id = int(match.group(1))
                dev_name = match.group(2).strip().lower()
                if any(sw in dev_name for sw in software_renderers):
                    logger.info("Skipping software Vulkan device %d: %s", dev_id, match.group(2).strip())
                    continue
                gpu_ids.append(dev_id)

            if gpu_ids:
                logger.info("Detected Real-ESRGAN Vulkan GPUs: %s", gpu_ids)
            else:
                logger.warning(
                    "Real-ESRGAN found no hardware Vulkan GPUs (only software renderers). "
                    "Ensure NVIDIA Vulkan ICD is installed (libnvidia-gl, nvidia-vulkan-icd)."
                )
            return gpu_ids
        except Exception as e:
            logger.warning("Failed to probe Real-ESRGAN Vulkan devices: %s", e)
            return []

    @staticmethod
    def _resolve_parallel_jobs(gpu_ids: list[int], expected_segments: int) -> int:
        """Choose how many Real-ESRGAN segment workers to run at once."""
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

    async def _upscale_frames(
        self,
        input_dir: Path,
        output_dir: Path,
        scale: int = 4,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_frames: int = 0,
        gpu_id: int = 0,
    ) -> None:
        """Dispatch frame upscaling to the active backend."""
        if self._backend == "pytorch":
            await self._upscale_frames_pytorch(
                input_dir, output_dir, scale, progress_callback, total_frames, gpu_id,
            )
        else:
            await self._upscale_frames_ncnn(
                input_dir, output_dir, scale, progress_callback, total_frames, gpu_id,
            )

    async def _upscale_frames_ncnn(
        self,
        input_dir: Path,
        output_dir: Path,
        scale: int = 4,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_frames: int = 0,
        gpu_id: int = 0,
    ) -> None:
        """Run Real-ESRGAN ncnn-vulkan binary on extracted frames."""
        output_format = Config.REALESRGAN_OUTPUT_FORMAT if Config.REALESRGAN_OUTPUT_FORMAT in {"jpg", "png", "webp"} else "jpg"
        cmd = [
            self._binary,
            "-i", str(input_dir),
            "-o", str(output_dir),
            "-n", ANIME_MODEL,
            "-s", str(scale),
            "-f", output_format,
            "-g", str(gpu_id),
            "-j", Config.REALESRGAN_THREADS,
        ]
        if Config.REALESRGAN_TILE_SIZE > 0:
            cmd.extend(["-t", str(Config.REALESRGAN_TILE_SIZE)])

        logger.info("Real-ESRGAN ncnn command: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communicate_task = asyncio.create_task(proc.communicate())
        output_glob = f"*.{output_format}"

        while not communicate_task.done():
            await asyncio.sleep(2)
            if progress_callback and total_frames > 0:
                done = len(list(output_dir.glob(output_glob)))
                try:
                    progress_callback(done, total_frames)
                except Exception:
                    pass

        stdout_data, stderr_data = await communicate_task
        if proc.returncode != 0:
            error_msg = stderr_data.decode("utf-8", errors="replace")
            raise RuntimeError(f"Real-ESRGAN ncnn failed (exit {proc.returncode}): {error_msg}")

        upscaled_count = len(list(output_dir.glob(output_glob)))
        if upscaled_count == 0:
            stdout_msg = stdout_data.decode("utf-8", errors="replace")
            stderr_msg = stderr_data.decode("utf-8", errors="replace")
            raise RuntimeError(f"Real-ESRGAN ncnn produced no output frames. stdout={stdout_msg[:300]} stderr={stderr_msg[:300]}")

        if progress_callback and total_frames > 0:
            progress_callback(upscaled_count, total_frames)
        logger.info("Upscaled %d frames on GPU %s (ncnn)", upscaled_count, gpu_id)

    async def _upscale_frames_pytorch(
        self,
        input_dir: Path,
        output_dir: Path,
        scale: int = 4,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_frames: int = 0,
        gpu_id: int = 0,
    ) -> None:
        """Run Real-ESRGAN via PyTorch/CUDA on extracted frames."""
        import cv2
        import numpy as np
        import torch
        from basicsr.archs.rrdbnet_arch import RRDBNet
        from realesrgan import RealESRGANer

        output_format = Config.REALESRGAN_OUTPUT_FORMAT if Config.REALESRGAN_OUTPUT_FORMAT in {"jpg", "png", "webp"} else "jpg"

        # Set CUDA device for this segment
        torch.cuda.set_device(gpu_id)
        device = torch.device(f"cuda:{gpu_id}")
        logger.info("Real-ESRGAN pytorch: GPU %d, scale %dx", gpu_id, scale)

        # Build model — realesr-animevideov3 uses a compact RRDBNet
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=scale)

        # RealESRGANer requires an explicit model_path (URL or local file)
        model_url = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
            "realesr-animevideov3.pth"
        )

        upsampler = RealESRGANer(
            scale=scale,
            model_path=model_url,
            model=model,
            tile=Config.REALESRGAN_TILE_SIZE if Config.REALESRGAN_TILE_SIZE > 0 else 0,
            tile_pad=10,
            pre_pad=0,
            half=True,  # fp16 for speed on consumer GPUs
            device=device,
        )
        # Re-create with explicit model_path
        import urllib.request
        model_dir = Path.home() / ".cache" / "realesrgan"
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "realesr-animevideov3.pth"
        if not model_path.exists():
            logger.info("Downloading realesr-animevideov3 model weights...")
            await asyncio.get_event_loop().run_in_executor(
                None, urllib.request.urlretrieve, model_url, str(model_path),
            )
            logger.info("Model downloaded to %s", model_path)

        upsampler = RealESRGANer(
            scale=scale,
            model_path=str(model_path),
            model=model,
            tile=Config.REALESRGAN_TILE_SIZE if Config.REALESRGAN_TILE_SIZE > 0 else 0,
            tile_pad=10,
            pre_pad=0,
            half=True,
            device=device,
        )

        frame_files = sorted(input_dir.glob("frame_*.jpg"))
        upscaled_count = 0

        for frame_file in frame_files:
            img = cv2.imread(str(frame_file), cv2.IMREAD_UNCHANGED)
            if img is None:
                logger.warning("Could not read frame: %s", frame_file)
                continue

            # RealESRGANer.enhance expects BGR numpy array
            output, _ = upsampler.enhance(img, outscale=scale)

            out_name = frame_file.stem + f".{output_format}"
            out_path = output_dir / out_name
            cv2.imwrite(str(out_path), output)
            upscaled_count += 1

            if progress_callback and total_frames > 0:
                try:
                    progress_callback(upscaled_count, total_frames)
                except Exception:
                    pass

        # Free VRAM
        del upsampler
        torch.cuda.empty_cache()

        if upscaled_count == 0:
            raise RuntimeError("Real-ESRGAN pytorch produced no output frames")

        if progress_callback and total_frames > 0:
            progress_callback(upscaled_count, total_frames)
        logger.info("Upscaled %d frames on GPU %s (pytorch)", upscaled_count, gpu_id)

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
                    "-i", str(frames_dir / f"frame_%08d.{frame_ext}"),
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
