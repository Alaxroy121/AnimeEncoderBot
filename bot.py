"""
AnimeEncoderBot — Simplified version.
Just send a video → choose settings → get result.
Supports batch mode for multiple videos.
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── User state ────────────────────────────────────────────────────────
user_state: dict[int, dict] = {}
# State structure:
# {
#     "mode": "single" | "batch",
#     "videos": [(file_id, file_name, file_size), ...],
#     "settings": {"resolution": "4k", "codec": "hevc", ...},
#     "processing": False
# }

def get_state(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {"mode": None, "videos": [], "settings": {}, "processing": False}
    return user_state[user_id]

def clear_state(user_id: int) -> None:
    user_state.pop(user_id, None)

# ── Welcome images ────────────────────────────────────────────────────
ASSETS_DIR = Path(__file__).parent / "assets"

def get_random_welcome_image() -> Path | None:
    images = sorted(ASSETS_DIR.glob("welcome_*.png"))
    return random.choice(images) if images else None

# ── Keyboards ─────────────────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu after receiving video(s)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Upscale Only", callback_data="action_upscale")],
        [InlineKeyboardButton("🔍🎬 Upscale + Encode (Best)", callback_data="action_upscale_encode")],
        [InlineKeyboardButton("🎬 Encode Only", callback_data="action_encode")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def resolution_keyboard(action: str) -> InlineKeyboardMarkup:
    """Resolution selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 1080p", callback_data=f"{action}_res_1080p"),
            InlineKeyboardButton("🖥 2K", callback_data=f"{action}_res_2k"),
        ],
        [
            InlineKeyboardButton("📽 4K", callback_data=f"{action}_res_4k"),
            InlineKeyboardButton("🎬 8K", callback_data=f"{action}_res_8k"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def codec_keyboard(action: str) -> InlineKeyboardMarkup:
    """Codec selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 HEVC (Fast, GPU)", callback_data=f"{action}_codec_hevc"),
        ],
        [
            InlineKeyboardButton("🎬 AV1 (Best quality, CPU)", callback_data=f"{action}_codec_av1"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def batch_keyboard(count: int) -> InlineKeyboardMarkup:
    """Batch mode keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Done ({count} videos)", callback_data="batch_done")],
        [InlineKeyboardButton("❌ Cancel Batch", callback_data="cancel")],
    ])

# ── Command Handlers ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    user = update.effective_user
    logger.info("/start from %s (%s)", user.first_name, user.id)

    try:
        await db.add_user(user.id, user.username or "")
    except Exception:
        pass

    gpu_status = f"✅ {encoder.gpu_name}" if encoder.gpu_available else "❌ CPU mode"

    welcome_text = (
        f"👋 **Hello {user.first_name}!**\n\n"
        f"🎬 I am **AnimeEncoderBot**\n"
        f"_AI-Powered Video Upscaling & Encoding_\n\n"
        f"**How to use:**\n"
        f"📹 Just send me a video file!\n"
        f"📦 Or use /batch for multiple videos\n\n"
        f"**Features:**\n"
        f"• 🔍 AI Upscaling (1080p → 8K)\n"
        f"• 🎬 HEVC/AV1 Encoding\n"
        f"• ⚡ GPU: {gpu_status}\n\n"
        f"Send a video to get started!"
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
        except Exception:
            pass

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start batch mode."""
    user_id = update.effective_user.id
    state = get_state(user_id)
    
    if state["processing"]:
        await update.message.reply_text("⚠️ Already processing. Please wait.")
        return

    state["mode"] = "batch"
    state["videos"] = []
    state["settings"] = {}

    await update.message.reply_text(
        "📦 **Batch Mode Started**\n\n"
        "Send me videos one by one.\n"
        "When done, press the **Done** button.\n\n"
        "Videos added: **0**",
        reply_markup=batch_keyboard(0),
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command."""
    await update.message.reply_text(
        "📖 **AnimeEncoderBot — Help**\n\n"
        "**Single Video:**\n"
        "Just send a video → choose action → done!\n\n"
        "**Batch Mode:**\n"
        "/batch → send multiple videos → Done → choose action\n\n"
        "**Actions:**\n"
        "• 🔍 **Upscale Only** — AI upscale to higher resolution\n"
        "• 🔍🎬 **Upscale + Encode** — Upscale then encode (best quality)\n"
        "• 🎬 **Encode Only** — Just re-encode with HEVC/AV1\n\n"
        "**Other Commands:**\n"
        "• /status — Check current task\n"
        "• /cancel — Cancel current operation\n",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Status command."""
    user_id = update.effective_user.id
    state = get_state(user_id)

    if state["processing"]:
        await update.message.reply_text("⚙️ Currently processing your video(s)...")
    elif state["videos"]:
        await update.message.reply_text(f"📦 {len(state['videos'])} video(s) waiting for settings.")
    else:
        await update.message.reply_text("📭 No active tasks. Send a video to start!")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel command."""
    user_id = update.effective_user.id
    clear_state(user_id)
    await update.message.reply_text("❌ Cancelled. Send a new video to start again.")


# ── File Handler ──────────────────────────────────────────────────────

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming video files."""
    user_id = update.effective_user.id
    message = update.message
    state = get_state(user_id)

    if state["processing"]:
        await message.reply_text("⚠️ Already processing. Please wait until current task finishes.")
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
        await message.reply_text(f"⚠️ Unsupported format: `{filename}`")
        return

    file_size = file.file_size or 0
    if file_size > Config.MAX_FILE_SIZE:
        await message.reply_text(f"⚠️ File too large: `{human_size(file_size)}`")
        return

    # Add to state
    video_info = (file.file_id, filename, file_size)

    if state["mode"] == "batch":
        # Batch mode — add to list
        state["videos"].append(video_info)
        count = len(state["videos"])
        await message.reply_text(
            f"✅ Added: `{filename}` ({human_size(file_size)})\n\n"
            f"Videos in batch: **{count}**\n\n"
            f"Send more or press **Done**.",
            reply_markup=batch_keyboard(count),
            parse_mode="Markdown",
        )
    else:
        # Single video mode
        state["mode"] = "single"
        state["videos"] = [video_info]
        await message.reply_text(
            f"📹 **Video Received**\n\n"
            f"📁 `{filename}`\n"
            f"💾 Size: `{human_size(file_size)}`\n\n"
            f"What do you want to do?",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )


# ── Callback Handler ──────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all button callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data
    state = get_state(user_id)

    # Cancel
    if data == "cancel":
        clear_state(user_id)
        await query.message.edit_text("❌ Cancelled.")
        return

    # Batch done
    if data == "batch_done":
        if not state["videos"]:
            await query.message.edit_text("⚠️ No videos added. Send videos first.")
            return
        count = len(state["videos"])
        await query.message.edit_text(
            f"📦 **{count} video(s) ready**\n\n"
            f"What do you want to do with all of them?",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # Action selection
    if data == "action_upscale":
        state["settings"]["action"] = "upscale"
        await query.message.edit_text(
            "🔍 **Upscale Only**\n\nChoose target resolution:",
            reply_markup=resolution_keyboard("upscale"),
            parse_mode="Markdown",
        )
        return

    if data == "action_upscale_encode":
        state["settings"]["action"] = "upscale_encode"
        await query.message.edit_text(
            "🔍🎬 **Upscale + Encode**\n\nChoose target resolution:",
            reply_markup=resolution_keyboard("upscale_encode"),
            parse_mode="Markdown",
        )
        return

    if data == "action_encode":
        state["settings"]["action"] = "encode"
        await query.message.edit_text(
            "🎬 **Encode Only**\n\nChoose codec:",
            reply_markup=codec_keyboard("encode"),
            parse_mode="Markdown",
        )
        return

    # Resolution selection
    if "_res_" in data:
        parts = data.split("_res_")
        action = parts[0]
        resolution = parts[1]
        state["settings"]["resolution"] = resolution

        if action == "upscale":
            # Upscale only — start processing
            await query.message.edit_text("⏳ Starting processing...")
            await start_processing(user_id, query.message, context.application)
        else:
            # Upscale + Encode — ask for codec
            await query.message.edit_text(
                f"🔍🎬 **Upscale + Encode**\n\n"
                f"📐 Resolution: **{resolution.upper()}**\n\n"
                f"Choose codec:",
                reply_markup=codec_keyboard("upscale_encode"),
                parse_mode="Markdown",
            )
        return

    # Codec selection
    if "_codec_" in data:
        parts = data.split("_codec_")
        codec = parts[1]
        state["settings"]["codec"] = codec
        state["settings"]["quality"] = "high"
        state["settings"]["preset"] = "medium"

        await query.message.edit_text("⏳ Starting processing...")
        await start_processing(user_id, query.message, context.application)
        return


# ── Processing ────────────────────────────────────────────────────────

async def start_processing(user_id: int, message, app: Application) -> None:
    """Start processing video(s)."""
    state = get_state(user_id)
    state["processing"] = True

    videos = state["videos"]
    settings = state["settings"]
    action = settings.get("action", "upscale")
    total = len(videos)

    logger.info("Processing %d video(s) for user %s, action=%s", total, user_id, action)

    for i, (file_id, filename, file_size) in enumerate(videos, 1):
        prefix = f"[{i}/{total}] " if total > 1 else ""

        try:
            # Update status
            await message.edit_text(
                f"{prefix}📥 **Downloading...**\n\n"
                f"📁 `{filename}`\n"
                f"💾 `{human_size(file_size)}`",
                parse_mode="Markdown",
            )

            # Download
            download_path = os.path.join(Config.DOWNLOAD_DIR, f"{user_id}_{filename}")
            tg_file = await app.bot.get_file(file_id)
            await tg_file.download_to_drive(download_path)

            # Process based on action
            if action == "upscale":
                output_path = await process_upscale(
                    download_path, settings, message, prefix, app
                )
            elif action == "upscale_encode":
                output_path = await process_upscale_encode(
                    download_path, settings, message, prefix, app
                )
            else:  # encode
                output_path = await process_encode(
                    download_path, settings, message, prefix, app
                )

            # Upload
            if output_path and Path(output_path).exists():
                await upload_result(output_path, message, prefix, app, user_id)
                cleanup_files(output_path)

            cleanup_files(download_path)

        except Exception as e:
            logger.error("Processing failed: %s\n%s", e, traceback.format_exc())
            await app.bot.send_message(
                user_id,
                f"{prefix}❌ **Failed**\n\n`{filename}`\n\nError: `{str(e)[:200]}`",
                parse_mode="Markdown",
            )

    # Done
    clear_state(user_id)
    await app.bot.send_message(
        user_id,
        f"✅ **All done!** Processed {total} video(s).\n\nSend more videos anytime!",
        parse_mode="Markdown",
    )


async def process_upscale(input_path: str, settings: dict, message, prefix: str, app) -> str:
    """Upscale only."""
    resolution = settings.get("resolution", "4k")

    async def on_progress(current: int, total: int) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await message.edit_text(
                f"{prefix}🔍 **Upscaling to {resolution.upper()}**\n\n"
                f"🖼 Frames: `{current}/{total}`\n"
                f"📊 Progress: `{percent:.1f}%`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    return await upscaler.upscale(
        input_path=input_path,
        target_resolution=resolution,
        progress_callback=on_progress,
    )


async def process_encode(input_path: str, settings: dict, message, prefix: str, app) -> str:
    """Encode only."""
    encode_settings = EncodeSettings(
        codec=settings.get("codec", "hevc"),
        quality=settings.get("quality", "high"),
        preset=settings.get("preset", "medium"),
        audio_codec="copy",
        use_gpu=Config.GPU_ENABLED,
    )

    async def on_progress(current: float, total: float) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await message.edit_text(
                f"{prefix}🎬 **Encoding with {encode_settings.codec.upper()}**\n\n"
                f"📊 Progress: `{percent:.1f}%`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    return await encoder.encode(
        input_path=input_path,
        settings=encode_settings,
        progress_callback=on_progress,
    )


async def process_upscale_encode(input_path: str, settings: dict, message, prefix: str, app) -> str:
    """Upscale then encode."""
    resolution = settings.get("resolution", "4k")
    codec = settings.get("codec", "hevc")

    # Step 1: Upscale
    async def on_upscale_progress(current: int, total: int) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await message.edit_text(
                f"{prefix}🔍 **Step 1: Upscaling to {resolution.upper()}**\n\n"
                f"🖼 Frames: `{current}/{total}`\n"
                f"📊 Progress: `{percent:.1f}%`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    upscaled_path = await upscaler.upscale(
        input_path=input_path,
        target_resolution=resolution,
        progress_callback=on_upscale_progress,
    )

    # Step 2: Encode
    encode_settings = EncodeSettings(
        codec=codec,
        quality=settings.get("quality", "high"),
        preset=settings.get("preset", "medium"),
        audio_codec="copy",
        use_gpu=Config.GPU_ENABLED,
    )

    async def on_encode_progress(current: float, total: float) -> None:
        percent = (current / total * 100) if total > 0 else 0
        try:
            await message.edit_text(
                f"{prefix}🎬 **Step 2: Encoding with {codec.upper()}**\n\n"
                f"📊 Progress: `{percent:.1f}%`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    encoded_path = await encoder.encode(
        input_path=upscaled_path,
        settings=encode_settings,
        progress_callback=on_encode_progress,
    )

    cleanup_files(upscaled_path)
    return encoded_path


async def upload_result(output_path: str, message, prefix: str, app, user_id: int) -> None:
    """Upload result to Telegram or GDrive."""
    file_size = Path(output_path).stat().st_size
    filename = Path(output_path).name

    try:
        await message.edit_text(
            f"{prefix}📤 **Uploading...**\n\n💾 `{human_size(file_size)}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    if file_size <= Config.TG_UPLOAD_LIMIT:
        # Telegram upload
        try:
            await app.bot.send_document(
                chat_id=user_id,
                document=open(output_path, "rb"),
                caption=f"✅ **Done!**\n\n📁 `{filename}`\n💾 `{human_size(file_size)}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Telegram upload failed: %s", e)
            await _upload_gdrive(output_path, file_size, app, user_id)
    else:
        await _upload_gdrive(output_path, file_size, app, user_id)


async def _upload_gdrive(output_path: str, file_size: int, app, user_id: int) -> None:
    """Upload to Google Drive."""
    if not gdrive.is_configured():
        await app.bot.send_message(
            user_id,
            f"❌ File too large ({human_size(file_size)}) and GDrive not configured.",
        )
        return

    try:
        result = await gdrive.upload(file_path=output_path, filename=Path(output_path).name)
        await app.bot.send_message(
            user_id,
            f"✅ **Uploaded to Google Drive!**\n\n"
            f"💾 `{human_size(file_size)}`\n\n"
            f"📁 {result['link']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await app.bot.send_message(user_id, f"❌ GDrive upload failed: `{str(e)[:200]}`")


# ── Startup ───────────────────────────────────────────────────────────

async def on_startup(app: Application) -> None:
    """Initialize services."""
    logger.info("=" * 60)
    logger.info("AnimeEncoderBot starting up...")
    logger.info("=" * 60)

    try:
        await db.connect()
    except Exception as e:
        logger.warning("MongoDB failed: %s", e)

    await encoder.initialize()
    logger.info("GPU: %s | HEVC: %s | AV1: %s",
                encoder.gpu_name, encoder.gpu_available, encoder.has_av1_nvenc)

    await upscaler.check_available()
    logger.info("Real-ESRGAN: %s", "available" if upscaler._available else "NOT FOUND")

    logger.info("GDrive: %s", "configured" if gdrive.is_configured() else "not configured")

    # Notify admins
    for admin_id in Config.ADMIN_IDS:
        try:
            await app.bot.send_message(
                admin_id,
                f"🟢 **Bot Online!**\n\n"
                f"🖥 GPU: {encoder.gpu_name if encoder.gpu_available else 'CPU'}\n"
                f"📹 Send a video to start!",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    logger.info("Bot is ready!")


def main() -> None:
    """Main entry point."""
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config: %s", err)
        sys.exit(1)

    Config.ensure_dirs()

    app = Application.builder().token(Config.BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("batch", cmd_batch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Videos
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video))

    app.post_init = on_startup

    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
