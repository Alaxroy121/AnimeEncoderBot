"""
AnimeEncoderBot — Main entry point.
Telegram bot for GPU-accelerated video encoding (AV1/HEVC) and AI anime upscaling.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from pyrogram import Client, filters, idle
from pyrogram.types import Message

from callbacks import register_callbacks, get_workflow, clear_workflow, set_workflow
from commands import register_commands
from config import Config
from database import db
from encoder import encoder, EncodeSettings
from gdrive import gdrive
from queue_manager import queue_manager, Task, TaskType, TaskStatus
from upscaler import upscaler
from utils import (
    cleanup_files,
    format_media_info,
    get_media_info,
    human_size,
    is_supported_video,
    ProgressTracker,
)

# ── Logging Setup ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Silence noisy loggers
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)


# ── Bot Initialization ────────────────────────────────────────────────

def create_app() -> Client:
    """Create and configure the Pyrogram client."""
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    Config.ensure_dirs()

    # Remove stale session files that can block Pyrogram from connecting
    session_file = Path(__file__).parent / "AnimeEncoderBot.session"
    session_journal = Path(__file__).parent / "AnimeEncoderBot.session-journal"
    for sf in [session_file, session_journal]:
        if sf.exists():
            sf.unlink()
            logger.info("Removed stale session file: %s", sf)

    app = Client(
        name="AnimeEncoderBot",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN,
        workdir=str(Path(__file__).parent),
        in_memory=True,  # Don't persist session to disk — avoids session conflicts
    )
    return app


app = create_app()


# ── Task Processor ────────────────────────────────────────────────────

async def process_task(task: Task) -> None:
    """Process an encoding or upscaling task.

    This is the main worker function called by QueueManager.
    """
    chat_id = task.progress_chat_id
    msg_id = task.progress_message_id
    input_file = task.input_file
    output_file: str = ""

    try:
        # Update progress message
        if chat_id and msg_id:
            await app.edit_message_text(
                chat_id, msg_id,
                f"⚙️ **{'Encoding' if task.task_type == TaskType.ENCODE else 'Upscaling'}...**\n\n"
                f"⏳ Starting up...",
            )

        if task.task_type == TaskType.ENCODE:
            output_file = await _process_encode(task)
        elif task.task_type == TaskType.UPSCALE:
            output_file = await _process_upscale(task)

        # Upload result
        if output_file and Path(output_file).exists():
            await _upload_result(task, output_file)
        else:
            raise RuntimeError("Processing produced no output file")

    except asyncio.CancelledError:
        if chat_id:
            await app.send_message(chat_id, "❌ Task cancelled.")
        raise

    except Exception as e:
        logger.error("Task %s failed: %s", task.task_id, e, exc_info=True)
        if chat_id:
            await app.send_message(
                chat_id,
                f"❌ **Task Failed**\n\n`{str(e)[:500]}`\n\n"
                f"Task ID: `{task.task_id}`",
            )
        raise

    finally:
        # Cleanup temp files
        cleanup_files(input_file)
        if output_file:
            cleanup_files(output_file)
        clear_workflow(task.user_id)


async def _process_encode(task: Task) -> str:
    """Run the encoding pipeline."""
    settings = EncodeSettings(
        codec=task.settings.get("codec", Config.DEFAULT_CODEC),
        quality=task.settings.get("quality", "medium"),
        preset=task.settings.get("preset", "medium"),
        audio_codec=task.settings.get("audio", "copy"),
        use_gpu=Config.GPU_ENABLED,
    )

    chat_id = task.progress_chat_id
    msg_id = task.progress_message_id

    # Progress callback
    tracker = ProgressTracker(total=100)

    def on_progress(current: float, total: float) -> None:
        tracker.total = total
        if tracker.update(current):
            text = tracker.format_progress("Encoding")
            if chat_id and msg_id:
                asyncio.get_event_loop().create_task(
                    _safe_edit(chat_id, msg_id, text)
                )

    output_path = await encoder.encode(
        input_path=task.input_file,
        settings=settings,
        progress_callback=on_progress,
    )

    await db.update_task(task.task_id, {"output_file": output_path})
    return output_path


async def _process_upscale(task: Task) -> str:
    """Run the upscaling pipeline."""
    resolution = task.settings.get("resolution", "4k")
    chat_id = task.progress_chat_id
    msg_id = task.progress_message_id

    total_frames_est = 1000  # Will be updated

    def on_progress(current: int, total: int) -> None:
        nonlocal total_frames_est
        total_frames_est = total
        percent = (current / total * 100) if total > 0 else 0
        text = (
            f"🔍 **Upscaling to {resolution.upper()}**\n\n"
            f"🖼 Frames: `{current}/{total}`\n"
            f"📊 Progress: `{percent:.1f}%`\n"
            f"🤖 Model: Real-ESRGAN Anime V3"
        )
        if chat_id and msg_id:
            asyncio.get_event_loop().create_task(
                _safe_edit(chat_id, msg_id, text)
            )

    output_path = await upscaler.upscale(
        input_path=task.input_file,
        target_resolution=resolution,
        progress_callback=on_progress,
    )

    await db.update_task(task.task_id, {"output_file": output_path})
    return output_path


async def _upload_result(task: Task, output_path: str) -> None:
    """Upload the result file.

    - Files < 2GB → Direct Telegram upload (Pyrogram handles up to 2GB with API ID/Hash)
    - Files > 2GB → Google Drive upload via Service Account, sends link in chat
    """
    chat_id = task.progress_chat_id
    if not chat_id:
        return

    file_size = Path(output_path).stat().st_size
    task_label = "Encoding" if task.task_type == TaskType.ENCODE else "Upscaling"

    # Get media info for the caption
    info = await get_media_info(output_path)
    info_lines = ""
    if info and info.get("video"):
        v = info["video"]
        info_lines = (
            f"\n📐 Resolution: `{v['width']}×{v['height']}`"
            f"\n🎬 Codec: `{v['codec']}`"
        )

    if file_size <= Config.TG_UPLOAD_LIMIT:
        # ── Direct Telegram upload (< 2GB) with progress ──
        caption = (
            f"✅ **{task_label} Complete!**\n\n"
            f"💾 Size: `{human_size(file_size)}`\n"
            f"🆔 Task: `{task.task_id}`"
            f"{info_lines}"
        )

        upload_msg = await app.send_message(
            chat_id,
            f"📤 **Uploading to Telegram...**\n\n"
            f"💾 Size: `{human_size(file_size)}`\n"
            f"⏳ Starting upload..."
        )

        ul_last_update = [0.0]

        async def upload_progress(current: int, total: int) -> None:
            import time
            now = time.time()
            if now - ul_last_update[0] < 3:
                return
            ul_last_update[0] = now
            percent = (current / total * 100) if total > 0 else 0
            bar_len = 20
            filled = int(bar_len * current / total) if total > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            await _safe_edit(
                chat_id, upload_msg.id,
                f"📤 **Uploading to Telegram...**\n\n"
                f"[{bar}] `{percent:.1f}%`\n"
                f"💾 `{human_size(current)}` / `{human_size(total)}`"
            )

        try:
            await app.send_document(
                chat_id,
                document=output_path,
                caption=caption,
                force_document=True,
                progress=upload_progress,
            )
            # Delete the progress message after successful upload
            try:
                await upload_msg.delete()
            except Exception:
                pass
        except Exception as e:
            logger.error("Telegram upload failed: %s", e)
            await upload_msg.edit_text(
                f"❌ Telegram upload failed: `{str(e)[:300]}`\n\n"
                "Trying Google Drive fallback...",
            )
            # Fall through to GDrive
            await _upload_to_gdrive(task, output_path, file_size, task_label, info_lines, chat_id)
    else:
        # ── Google Drive upload (> 2GB) ──
        await _upload_to_gdrive(task, output_path, file_size, task_label, info_lines, chat_id)

    # Log to channel
    if Config.LOG_CHANNEL:
        try:
            method = "Telegram" if file_size <= Config.TG_UPLOAD_LIMIT else "GDrive"
            await app.send_message(
                Config.LOG_CHANNEL,
                f"✅ Task completed\n"
                f"👤 User: `{task.user_id}`\n"
                f"📋 Type: `{task.task_type.value}`\n"
                f"💾 Size: `{human_size(file_size)}`\n"
                f"📤 Upload: `{method}`\n"
                f"🆔 Task: `{task.task_id}`",
            )
        except Exception as e:
            logger.warning("Failed to log to channel: %s", e)


async def _upload_to_gdrive(
    task: Task,
    output_path: str,
    file_size: int,
    task_label: str,
    info_lines: str,
    chat_id: int,
) -> None:
    """Upload a file to Google Drive and send the link."""
    if not gdrive.is_configured():
        await app.send_message(
            chat_id,
            f"❌ **File too large for Telegram** (`{human_size(file_size)}`)\n\n"
            "Google Drive is not configured.\n"
            "Ask the admin to set `GDRIVE_SA_JSON` and `GDRIVE_FOLDER_ID` in config.",
        )
        return

    progress_msg = await app.send_message(
        chat_id,
        f"📤 **Uploading to Google Drive...**\n\n"
        f"💾 Size: `{human_size(file_size)}`\n"
        f"⏳ This may take a while for large files...",
    )

    last_update = [0.0]

    def on_gdrive_progress(uploaded: int, total: int) -> None:
        import time
        now = time.time()
        if now - last_update[0] < 5:  # Throttle updates to every 5s
            return
        last_update[0] = now
        percent = (uploaded / total * 100) if total > 0 else 0
        asyncio.get_event_loop().create_task(
            _safe_edit(
                chat_id, progress_msg.id,
                f"📤 **Uploading to Google Drive...**\n\n"
                f"💾 `{human_size(uploaded)}` / `{human_size(total)}`\n"
                f"📊 `{percent:.1f}%`",
            )
        )

    try:
        result = await gdrive.upload(
            file_path=output_path,
            filename=Path(output_path).name,
            progress_callback=on_gdrive_progress,
        )

        await progress_msg.edit_text(
            f"✅ **{task_label} Complete!**\n\n"
            f"💾 Size: `{human_size(file_size)}`\n"
            f"🆔 Task: `{task.task_id}`"
            f"{info_lines}\n\n"
            f"📁 **Google Drive Link:**\n"
            f"{result['link']}",
        )

    except Exception as e:
        logger.error("GDrive upload failed for task %s: %s", task.task_id, e, exc_info=True)
        await progress_msg.edit_text(
            f"❌ **Google Drive upload failed**\n\n"
            f"`{str(e)[:400]}`\n\n"
            f"File size: `{human_size(file_size)}`",
        )


async def _safe_edit(chat_id: int, message_id: int, text: str) -> None:
    """Edit a message, ignoring errors (flood, not modified, etc.)."""
    try:
        await app.edit_message_text(chat_id, message_id, text)
    except Exception:
        pass


# ── File Handler (receives videos after workflow setup) ───────────────

@app.on_message(filters.private & (filters.video | filters.document | filters.animation))
async def on_file_received(client: Client, message: Message) -> None:
    """Handle incoming video files."""
    user_id = message.from_user.id

    # Check ban
    if await db.is_banned(user_id):
        await message.reply_text("🚫 You are banned from using this bot.")
        return

    # Check workflow
    wf = get_workflow(user_id)
    if not wf or not wf.get("awaiting_file"):
        await message.reply_text(
            "💡 Use /encode or /upscale first to set up your workflow, "
            "then send the video file."
        )
        return

    # Validate file
    file = message.video or message.document or message.animation
    if not file:
        await message.reply_text("⚠️ Please send a video file.")
        return

    filename = getattr(file, "file_name", None) or "video.mp4"
    if not is_supported_video(filename):
        await message.reply_text(
            f"⚠️ Unsupported format: `{filename}`\n\n"
            f"Supported: {', '.join(Config.SUPPORTED_EXTENSIONS)}"
        )
        return

    file_size = file.file_size or 0
    if file_size > Config.MAX_FILE_SIZE:
        await message.reply_text(
            f"⚠️ File too large: `{human_size(file_size)}`\n"
            f"Max: `{human_size(Config.MAX_FILE_SIZE)}`"
        )
        return

    # Check for existing active task
    active = await db.get_user_active_task(user_id)
    if active:
        await message.reply_text(
            "⚠️ You already have an active task.\n"
            "Use /cancel to cancel it, or /status to check progress."
        )
        return

    # ── Download with progress ──
    progress_msg = await message.reply_text(
        f"📥 **Downloading...**\n\n"
        f"💾 Size: `{human_size(file_size)}`\n"
        f"⏳ Starting download..."
    )

    dl_last_update = [0.0]

    async def download_progress(current: int, total: int) -> None:
        import time
        now = time.time()
        if now - dl_last_update[0] < 3:  # Throttle to every 3s
            return
        dl_last_update[0] = now
        percent = (current / total * 100) if total > 0 else 0
        bar_len = 20
        filled = int(bar_len * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        await _safe_edit(
            message.chat.id, progress_msg.id,
            f"📥 **Downloading...**\n\n"
            f"[{bar}] `{percent:.1f}%`\n"
            f"💾 `{human_size(current)}` / `{human_size(total)}`"
        )

    try:
        download_path = os.path.join(Config.DOWNLOAD_DIR, f"{user_id}_{filename}")
        await message.download(file_name=download_path, progress=download_progress)
    except Exception as e:
        await progress_msg.edit_text(f"❌ Download failed: `{str(e)[:200]}`")
        clear_workflow(user_id)
        return

    await progress_msg.edit_text(
        f"📥 **Download complete!** ✅\n\n"
        f"💾 Size: `{human_size(file_size)}`\n"
        f"🔍 Analyzing media..."
    )

    # Show media info
    info = await get_media_info(download_path)
    if info:
        info_text = format_media_info(info)
        await message.reply_text(info_text)

    # Create and queue the task
    task_type = TaskType.ENCODE if wf["type"] == "encode" else TaskType.UPSCALE
    is_admin = user_id in Config.ADMIN_IDS

    task = Task(
        task_id=Task.generate_id(),
        user_id=user_id,
        task_type=task_type,
        input_file=download_path,
        settings=wf,
        priority=10 if is_admin else 0,
        progress_message_id=progress_msg.id,
        progress_chat_id=message.chat.id,
    )

    await queue_manager.add_task(task)
    position = await db.get_queue_position(task.task_id)

    await progress_msg.edit_text(
        f"✅ **Task Queued**\n\n"
        f"🆔 Task ID: `{task.task_id}`\n"
        f"📋 Type: **{task_type.value.capitalize()}**\n"
        f"📊 Queue Position: **#{position}**\n\n"
        f"Use /status to check progress or /cancel to abort."
    )

    # Log to channel
    if Config.LOG_CHANNEL:
        try:
            await app.send_message(
                Config.LOG_CHANNEL,
                f"📋 New task queued\n"
                f"👤 User: `{user_id}` (@{message.from_user.username or 'N/A'})\n"
                f"📋 Type: `{task_type.value}`\n"
                f"💾 Size: `{human_size(file_size)}`\n"
                f"🆔 Task: `{task.task_id}`",
            )
        except Exception:
            pass


# ── Startup & Shutdown ────────────────────────────────────────────────

async def on_startup() -> None:
    """Initialize services on bot startup."""
    logger.info("=" * 60)
    logger.info("AnimeEncoderBot starting up...")
    logger.info("=" * 60)

    # Connect to MongoDB
    await db.connect()

    # Initialize encoder (GPU detection + verification)
    await encoder.initialize()
    logger.info("GPU: %s | HEVC NVENC: %s | AV1 NVENC: %s",
                encoder.gpu_name, encoder.gpu_available, encoder.has_av1_nvenc)

    # Verify GPU actually works (not just detected)
    if encoder.gpu_available:
        from utils import verify_gpu_encoding
        gpu_works = await verify_gpu_encoding([])
        if not gpu_works:
            logger.error("⚠️ GPU detected but NVENC test FAILED — encoding may fall back to CPU")

    # Check upscaler
    upscaler_ok = await upscaler.check_available()
    logger.info("Real-ESRGAN: %s", "available" if upscaler_ok else "NOT FOUND")

    # Check GDrive
    gdrive_ok = gdrive.is_configured()
    logger.info("Google Drive: %s", "configured" if gdrive_ok else "not configured (files >2GB won't upload)")

    # Start queue manager
    queue_manager.set_processor(process_task)
    await queue_manager.start()

    logger.info("Bot is ready!")


async def on_shutdown() -> None:
    """Clean shutdown."""
    logger.info("Shutting down...")
    await queue_manager.stop()
    await db.close()
    logger.info("Goodbye!")


# ── Main ──────────────────────────────────────────────────────────────

async def main() -> None:
    """Main async entry point."""
    # Register handlers
    register_commands(app)
    register_callbacks(app)

    await on_startup()

    try:
        await app.start()
        me = await app.get_me()
        logger.info("Bot started as @%s", me.username)

        # Self-test: send a ping to admins so they know bot is alive
        for admin_id in Config.ADMIN_IDS:
            try:
                await app.send_message(
                    admin_id,
                    f"🟢 **Bot Online!**\n\n"
                    f"🤖 @{me.username} is ready.\n"
                    f"🖥 GPU: {encoder.gpu_name if encoder.gpu_available else 'CPU mode'}\n"
                    f"⏰ Send /start to begin!",
                )
            except Exception:
                pass

        # Notify log channel
        if Config.LOG_CHANNEL:
            try:
                gpu_status = f"GPU: {encoder.gpu_name}" if encoder.gpu_available else "GPU: None (CPU mode)"
                gdrive_status = "✅ Configured" if gdrive.is_configured() else "❌ Not configured"
                await app.send_message(
                    Config.LOG_CHANNEL,
                    f"🟢 **Bot Started**\n\n"
                    f"🖥 {gpu_status}\n"
                    f"📁 GDrive: {gdrive_status}\n"
                    f"🔧 Workers: {Config.CONCURRENT_TASKS}\n"
                    f"📦 Max file: {human_size(Config.MAX_FILE_SIZE)}",
                )
            except Exception:
                pass

        await idle()
    finally:
        await on_shutdown()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
