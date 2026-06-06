"""
AnimeEncoderBot — Main entry point.
Telegram bot for GPU-accelerated video encoding (AV1/HEVC) and AI anime upscaling.
Uses python-telegram-bot (Bot API) for reliable message handling.
"""

import asyncio
import logging
import os
import random
import sys
import traceback
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── User workflow state (in-memory) ───────────────────────────────────
user_workflows: dict[int, dict] = {}

def get_workflow(user_id: int) -> dict | None:
    return user_workflows.get(user_id)

def set_workflow(user_id: int, data: dict) -> None:
    user_workflows[user_id] = data

def clear_workflow(user_id: int) -> None:
    user_workflows.pop(user_id, None)

# ── Welcome images ────────────────────────────────────────────────────
ASSETS_DIR = Path(__file__).parent / "assets"

def get_random_welcome_image() -> Path | None:
    images = sorted(ASSETS_DIR.glob("welcome_*.png"))
    return random.choice(images) if images else None

# ── Keyboards ─────────────────────────────────────────────────────────
def codec_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 AV1 (SVT-AV1)", callback_data="codec_av1"),
            InlineKeyboardButton("🎬 HEVC (H.265)", callback_data="codec_hevc"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])

def quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Low (Fast)", callback_data="quality_low"),
            InlineKeyboardButton("⚖️ Medium", callback_data="quality_medium"),
        ],
        [
            InlineKeyboardButton("✨ High", callback_data="quality_high"),
            InlineKeyboardButton("💎 Ultra (Slow)", callback_data="quality_ultra"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])

def preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏃 Fast", callback_data="preset_fast"),
            InlineKeyboardButton("⚖️ Medium", callback_data="preset_medium"),
        ],
        [
            InlineKeyboardButton("🐢 Slow", callback_data="preset_slow"),
            InlineKeyboardButton("🐌 Very Slow", callback_data="preset_veryslow"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])

def audio_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy (No Re-encode)", callback_data="audio_copy")],
        [
            InlineKeyboardButton("🔊 AAC 192k", callback_data="audio_aac"),
            InlineKeyboardButton("🔊 Opus 192k", callback_data="audio_opus"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])

def resolution_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 1080p (FHD)", callback_data="res_1080p"),
            InlineKeyboardButton("🖥 2K (QHD)", callback_data="res_2k"),
        ],
        [
            InlineKeyboardButton("📽 4K (UHD)", callback_data="res_4k"),
            InlineKeyboardButton("🎬 8K (FUHD)", callback_data="res_8k"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])

# ── Command Handlers ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with anime waifu image."""
    user = update.effective_user
    logger.info("/start from %s (%s)", user.first_name, user.id)

    try:
        await db.add_user(user.id, user.username or "")
    except Exception as e:
        logger.warning("DB add_user failed (non-fatal): %s", e)

    gpu_status = f"✅ {encoder.gpu_name}" if encoder.gpu_available else "❌ CPU mode"
    upscaler_status = "✅ Available" if upscaler._available else "❌ Not installed"

    welcome_text = (
        f"👋 **Hello {user.first_name}!**\n\n"
        f"🎬 I am **AnimeEncoderBot**\n"
        f"_Professional AI-Enhanced Video Encoding._\n\n"
        f"• 🧠 AI Upscaling: Real-ESRGAN (Anime V3)\n"
        f"• 🎬 Codecs: H.265 (HEVC) / AV1\n"
        f"• 📺 Resolution: Up to 8K\n"
        f"• ⚡ GPU: {gpu_status}\n"
        f"• 🔍 Upscaler: {upscaler_status}\n\n"
        f"**📋 Quick Start**\n"
        f"├ /encode — Encode video (AV1 / HEVC)\n"
        f"├ /upscale — AI upscale anime video\n"
        f"├ /help — Full command list\n"
        f"└ /settings — Your preferences"
    )

    welcome_img = get_random_welcome_image()
    if welcome_img and welcome_img.exists():
        try:
            await update.message.reply_photo(
                photo=open(welcome_img, "rb"),
                caption=welcome_text,
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.warning("Failed to send welcome image: %s", e)

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed help."""
    await update.message.reply_text(
        "📖 **AnimeEncoderBot — Help**\n\n"
        "**Encoding Commands**\n"
        "├ /encode — Start encoding workflow\n"
        "│   Choose codec → quality → preset → audio → send video\n"
        "│   Codecs: **AV1** (SVT-AV1) or **HEVC** (H.265 NVENC)\n\n"
        "**Upscaling Commands**\n"
        "├ /upscale — AI upscale (anime optimized)\n"
        "│   Choose resolution → send video\n"
        "│   Targets: 1080p, 2K, 4K, 8K\n"
        "│   Model: Real-ESRGAN Anime V3\n\n"
        "**General Commands**\n"
        "├ /status — Check your current task\n"
        "├ /cancel — Cancel your active task\n"
        "├ /queue — View the task queue\n"
        "├ /settings — Your default preferences\n\n"
        f"**Supported formats:** MP4, MKV, AVI, MOV, WebM, etc.\n"
        f"**Max file size:** {human_size(Config.MAX_FILE_SIZE)}",
        parse_mode="Markdown",
    )


async def cmd_encode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start encoding workflow."""
    user_id = update.effective_user.id

    try:
        if await db.is_banned(user_id):
            await update.message.reply_text("🚫 You are banned.")
            return
    except Exception:
        pass

    set_workflow(user_id, {"type": "encode", "awaiting_file": False})

    await update.message.reply_text(
        "🎬 **Video Encoding**\n\nChoose your codec:",
        reply_markup=codec_keyboard(),
        parse_mode="Markdown",
    )


async def cmd_upscale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start upscaling workflow."""
    user_id = update.effective_user.id

    try:
        if await db.is_banned(user_id):
            await update.message.reply_text("🚫 You are banned.")
            return
    except Exception:
        pass

    if not upscaler._available:
        await update.message.reply_text(
            "❌ **Upscaler Not Available**\n\n"
            "Real-ESRGAN is not installed on this server."
        )
        return

    set_workflow(user_id, {"type": "upscale", "awaiting_file": False})

    await update.message.reply_text(
        "🔍 **AI Anime Upscaling**\n\n"
        "Using **Real-ESRGAN Anime V3** model.\n\n"
        "Choose target resolution:",
        reply_markup=resolution_keyboard(),
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check current task status."""
    user_id = update.effective_user.id
    try:
        active = await db.get_user_active_task(user_id)
    except Exception:
        active = None

    if not active:
        await update.message.reply_text("📭 No active tasks. Use /encode or /upscale to start.")
        return

    status = active.get("status", "unknown")
    await update.message.reply_text(
        f"📋 **Task Status**\n\n"
        f"🆔 ID: `{active['task_id']}`\n"
        f"📋 Type: **{active.get('type', 'unknown')}**\n"
        f"📊 Status: **{status}**",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel active task."""
    user_id = update.effective_user.id
    clear_workflow(user_id)

    try:
        active = await db.get_user_active_task(user_id)
        if active:
            await queue_manager.cancel_task(active["task_id"])
            await update.message.reply_text(f"✅ Task `{active['task_id']}` cancelled.")
            return
    except Exception:
        pass

    await update.message.reply_text("📭 No active task to cancel.")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the task queue."""
    try:
        info = await queue_manager.get_queue_info()
        await update.message.reply_text(
            f"📋 **Task Queue**\n\n"
            f"⏳ Queued: **{info['queued']}**\n"
            f"⚙️ Processing: **{info['processing']}**\n"
            f"👷 Workers: **{info['total_workers']}**",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


# ── Callback Query Handlers ───────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data
    wf = get_workflow(user_id)

    if data == "cancel_workflow":
        clear_workflow(user_id)
        await query.message.edit_text("❌ Workflow cancelled.")
        return

    if not wf:
        await query.message.edit_text("⚠️ No active workflow. Use /encode or /upscale first.")
        return

    # ── Encoding flow ──
    if data.startswith("codec_"):
        wf["codec"] = data.replace("codec_", "")
        set_workflow(user_id, wf)
        await query.message.edit_text(
            f"🎬 Codec: **{wf['codec'].upper()}**\n\nChoose quality level:",
            reply_markup=quality_keyboard(),
            parse_mode="Markdown",
        )

    elif data.startswith("quality_"):
        wf["quality"] = data.replace("quality_", "")
        set_workflow(user_id, wf)
        await query.message.edit_text(
            f"🎬 Codec: **{wf['codec'].upper()}**\n"
            f"✨ Quality: **{wf['quality'].capitalize()}**\n\n"
            f"Choose encoding speed preset:",
            reply_markup=preset_keyboard(),
            parse_mode="Markdown",
        )

    elif data.startswith("preset_"):
        wf["preset"] = data.replace("preset_", "")
        set_workflow(user_id, wf)
        await query.message.edit_text(
            f"🎬 Codec: **{wf['codec'].upper()}**\n"
            f"✨ Quality: **{wf['quality'].capitalize()}**\n"
            f"🏃 Preset: **{wf['preset'].capitalize()}**\n\n"
            f"Choose audio handling:",
            reply_markup=audio_keyboard(),
            parse_mode="Markdown",
        )

    elif data.startswith("audio_"):
        wf["audio"] = data.replace("audio_", "")
        wf["awaiting_file"] = True
        set_workflow(user_id, wf)
        await query.message.edit_text(
            f"📋 **Encoding Settings**\n\n"
            f"🎬 Codec: **{wf['codec'].upper()}**\n"
            f"✨ Quality: **{wf['quality'].capitalize()}**\n"
            f"🏃 Preset: **{wf['preset'].capitalize()}**\n"
            f"🔊 Audio: **{wf['audio'].upper()}**\n\n"
            f"✅ **Now send your video file!**",
            parse_mode="Markdown",
        )

    # ── Upscaling flow ──
    elif data.startswith("res_"):
        wf["resolution"] = data.replace("res_", "")
        wf["awaiting_file"] = True
        set_workflow(user_id, wf)
        res_labels = {"1080p": "1920×1080", "2k": "2560×1440", "4k": "3840×2160", "8k": "7680×4320"}
        await query.message.edit_text(
            f"🔍 **Upscaling Settings**\n\n"
            f"📐 Target: **{wf['resolution'].upper()}** ({res_labels.get(wf['resolution'], '')})\n"
            f"🤖 Model: **Real-ESRGAN (Anime V3)**\n\n"
            f"✅ **Now send your video file!**",
            parse_mode="Markdown",
        )


# ── File Handler ──────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming video files."""
    user_id = update.effective_user.id
    message = update.message

    wf = get_workflow(user_id)
    if not wf or not wf.get("awaiting_file"):
        await message.reply_text(
            "💡 Use /encode or /upscale first to set up your workflow, "
            "then send the video file."
        )
        return

    # Get file info
    if message.video:
        file = message.video
        filename = file.file_name or "video.mp4"
    elif message.document:
        file = message.document
        filename = file.file_name or "video.mp4"
    else:
        await message.reply_text("⚠️ Please send a video file.")
        return

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

    # Download with progress
    progress_msg = await message.reply_text(
        f"📥 **Downloading...**\n\n"
        f"💾 Size: `{human_size(file_size)}`\n"
        f"⏳ Starting download..."
    )

    try:
        download_path = os.path.join(Config.DOWNLOAD_DIR, f"{user_id}_{filename}")
        tg_file = await file.get_file()
        await tg_file.download_to_drive(download_path)
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
        await message.reply_text(info_text, parse_mode="Markdown")

    # Queue the task
    task_type = TaskType.ENCODE if wf["type"] == "encode" else TaskType.UPSCALE
    is_admin = user_id in Config.ADMIN_IDS

    task = Task(
        task_id=Task.generate_id(),
        user_id=user_id,
        task_type=task_type,
        input_file=download_path,
        settings=wf,
        priority=10 if is_admin else 0,
        progress_message_id=progress_msg.message_id,
        progress_chat_id=message.chat_id,
    )

    try:
        await queue_manager.add_task(task)
        position = await db.get_queue_position(task.task_id)
    except Exception as e:
        logger.error("Failed to queue task: %s", e)
        position = 1

    await progress_msg.edit_text(
        f"✅ **Task Queued**\n\n"
        f"🆔 Task ID: `{task.task_id}`\n"
        f"📋 Type: **{task_type.value.capitalize()}**\n"
        f"📊 Queue Position: **#{position}**\n\n"
        f"Use /status to check progress or /cancel to abort."
    )

    clear_workflow(user_id)


# ── Task Processor ────────────────────────────────────────────────────

async def process_task(task: Task, app: Application) -> None:
    """Process an encoding or upscaling task."""
    chat_id = task.progress_chat_id
    msg_id = task.progress_message_id
    input_file = task.input_file
    output_file = ""

    try:
        if task.task_type == TaskType.ENCODE:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="⚙️ **Encoding...**\n\n⏳ Starting up...",
            )
            output_file = await _process_encode(task, app)
        else:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="🔍 **Upscaling...**\n\n⏳ Starting up...",
            )
            output_file = await _process_upscale(task, app)

        if output_file and Path(output_file).exists():
            await _upload_result(task, output_file, app)
        else:
            raise RuntimeError("Processing produced no output file")

    except asyncio.CancelledError:
        await app.bot.send_message(chat_id, "❌ Task cancelled.")
        raise
    except Exception as e:
        logger.error("Task %s failed: %s\n%s", task.task_id, e, traceback.format_exc())
        await app.bot.send_message(
            chat_id,
            f"❌ **Task Failed**\n\n`{str(e)[:500]}`\n\nTask ID: `{task.task_id}`",
        )
        raise
    finally:
        cleanup_files(input_file)
        if output_file:
            cleanup_files(output_file)


async def _process_encode(task: Task, app: Application) -> str:
    """Run encoding."""
    settings = EncodeSettings(
        codec=task.settings.get("codec", Config.DEFAULT_CODEC),
        quality=task.settings.get("quality", "medium"),
        preset=task.settings.get("preset", "medium"),
        audio_codec=task.settings.get("audio", "copy"),
        use_gpu=Config.GPU_ENABLED,
    )

    async def on_progress(current: float, total: float) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await app.bot.edit_message_text(
                chat_id=task.progress_chat_id,
                message_id=task.progress_message_id,
                text=f"⚙️ **Encoding...**\n\n📊 Progress: `{percent:.1f}%`",
            )
        except Exception:
            pass

    return await encoder.encode(
        input_path=task.input_file,
        settings=settings,
        progress_callback=on_progress,
    )


async def _process_upscale(task: Task, app: Application) -> str:
    """Run upscaling."""
    resolution = task.settings.get("resolution", "4k")

    async def on_progress(current: int, total: int) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await app.bot.edit_message_text(
                chat_id=task.progress_chat_id,
                message_id=task.progress_message_id,
                text=f"🔍 **Upscaling to {resolution.upper()}**\n\n"
                     f"🖼 Frames: `{current}/{total}`\n"
                     f"📊 Progress: `{percent:.1f}%`",
            )
        except Exception:
            pass

    return await upscaler.upscale(
        input_path=task.input_file,
        target_resolution=resolution,
        progress_callback=on_progress,
    )


async def _upload_result(task: Task, output_path: str, app: Application) -> None:
    """Upload result to Telegram or GDrive."""
    chat_id = task.progress_chat_id
    file_size = Path(output_path).stat().st_size
    task_label = "Encoding" if task.task_type == TaskType.ENCODE else "Upscaling"

    info = await get_media_info(output_path)
    info_lines = ""
    if info and info.get("video"):
        v = info["video"]
        info_lines = f"\n📐 Resolution: `{v['width']}×{v['height']}`\n🎬 Codec: `{v['codec']}`"

    if file_size <= Config.TG_UPLOAD_LIMIT:
        caption = (
            f"✅ **{task_label} Complete!**\n\n"
            f"💾 Size: `{human_size(file_size)}`\n"
            f"🆔 Task: `{task.task_id}`{info_lines}"
        )
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=task.progress_message_id,
            text=f"📤 **Uploading to Telegram...**\n\n💾 `{human_size(file_size)}`",
        )
        try:
            await app.bot.send_document(
                chat_id=chat_id,
                document=open(output_path, "rb"),
                caption=caption,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Telegram upload failed: %s", e)
            await _upload_to_gdrive(task, output_path, file_size, task_label, info_lines, app)
    else:
        await _upload_to_gdrive(task, output_path, file_size, task_label, info_lines, app)


async def _upload_to_gdrive(task, output_path, file_size, task_label, info_lines, app) -> None:
    """Upload to Google Drive."""
    chat_id = task.progress_chat_id

    if not gdrive.is_configured():
        await app.bot.send_message(
            chat_id,
            f"❌ **File too large** (`{human_size(file_size)}`)\n\n"
            "Google Drive is not configured.",
        )
        return

    await app.bot.edit_message_text(
        chat_id=chat_id,
        message_id=task.progress_message_id,
        text=f"📤 **Uploading to Google Drive...**\n\n💾 `{human_size(file_size)}`",
    )

    try:
        result = await gdrive.upload(file_path=output_path, filename=Path(output_path).name)
        await app.bot.send_message(
            chat_id,
            f"✅ **{task_label} Complete!**\n\n"
            f"💾 Size: `{human_size(file_size)}`\n"
            f"🆔 Task: `{task.task_id}`{info_lines}\n\n"
            f"📁 **Google Drive Link:**\n{result['link']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("GDrive upload failed: %s", e)
        await app.bot.send_message(chat_id, f"❌ **GDrive upload failed**\n\n`{str(e)[:300]}`")


# ── Startup ───────────────────────────────────────────────────────────

async def on_startup(app: Application) -> None:
    """Initialize services."""
    logger.info("=" * 60)
    logger.info("AnimeEncoderBot starting up...")
    logger.info("=" * 60)

    # MongoDB (non-fatal)
    try:
        await db.connect()
    except Exception as e:
        logger.error("MongoDB connection FAILED: %s — bot will work but tasks won't persist", e)

    # Encoder
    await encoder.initialize()
    logger.info("GPU: %s | HEVC NVENC: %s | AV1 NVENC: %s",
                encoder.gpu_name, encoder.gpu_available, encoder.has_av1_nvenc)

    # Upscaler
    await upscaler.check_available()
    logger.info("Real-ESRGAN: %s", "available" if upscaler._available else "NOT FOUND")

    # GDrive
    gdrive_ok = gdrive.is_configured()
    logger.info("Google Drive: %s", "configured" if gdrive_ok else "not configured")

    # Notify admins
    for admin_id in Config.ADMIN_IDS:
        try:
            await app.bot.send_message(
                admin_id,
                f"🟢 **Bot Online!**\n\n"
                f"🤖 @{(await app.bot.get_me()).username} is ready.\n"
                f"🖥 GPU: {encoder.gpu_name if encoder.gpu_available else 'CPU mode'}\n"
                f"⏰ Send /start to begin!",
            )
        except Exception:
            pass

    logger.info("Bot is ready!")


async def on_shutdown(app: Application) -> None:
    """Clean shutdown."""
    logger.info("Shutting down...")
    try:
        await db.close()
    except Exception:
        pass
    logger.info("Goodbye!")


# ── Main ──────────────────────────────────────────────────────────────

def main() -> None:
    """Main entry point."""
    # Validate config
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    Config.ensure_dirs()

    # Build application
    app = Application.builder().token(Config.BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("encode", cmd_encode))
    app.add_handler(CommandHandler("upscale", cmd_upscale))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_file))

    # Startup/shutdown hooks
    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Starting bot with python-telegram-bot (Bot API)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
