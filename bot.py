"""
AnimeEncoderBot — Main Pyrogram Entry Point.
Fully asynchronous, optimized with Pyrogram MTProto API.
"""

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import Config
from database import db
from encoder import encoder, EncodeSettings
from queue_manager import queue_manager, Task, TaskType, TaskStatus
from upscaler import upscaler
from gdrive import gdrive
from telegram_helpers import safe_edit_message_text, safe_edit_text
from utils import (
    cleanup_files,
    format_media_info,
    get_media_info,
    human_size,
    is_supported_video,
    ProgressTracker,
    get_gpu_count,
)

from commands import register_commands
from callbacks import (
    register_callbacks,
    get_workflow,
    set_workflow,
    clear_workflow,
    resolution_keyboard,
    codec_keyboard,
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
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# ── Pyrogram Client ───────────────────────────────────────────────────

app = Client(
    name="anime_encoder_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=16,
    sleep_threshold=30,
)

# ── Task Processor ────────────────────────────────────────────────────

async def process_task(task: Task) -> None:
    """The main processing function executed by queue_manager workers."""
    try:
        # Detect GPU mapping
        gpu_count = await get_gpu_count()
        gpu_id = (task.worker_id % gpu_count) if (task.worker_id is not None and gpu_count > 0) else 0
        logger.info(f"Task {task.task_id} assigned to worker {task.worker_id} (GPU {gpu_id})")

        # Create an async progress callback for Pyrogram upload/download
        async def pyrogram_progress(current: int, total: int) -> None:
            if not total:
                return
            if tracker.update(current): # Returns True if it's time to update
                try:
                    await safe_edit_message_text(
                        app,
                        chat_id=task.progress_chat_id,
                        message_id=task.progress_message_id,
                        text=tracker.format_progress(task_action),
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

        # 1. Download
        task_action = "Downloading"
        tracker = ProgressTracker(task.settings.get("file_size", 0))
        input_path = os.path.join(Config.DOWNLOAD_DIR, f"{task.task_id}_input{Path(task.settings.get('file_name', '.mp4')).suffix}")
        
        await safe_edit_message_text(
            app,
            chat_id=task.progress_chat_id,
            message_id=task.progress_message_id,
            text=f"📥 **Downloading...**\n\n📁 `{task.settings.get('file_name')}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        message_with_file = await app.get_messages(task.user_id, task.settings.get("message_id"))
        downloaded_file = await app.download_media(
            message_with_file,
            file_name=input_path,
            progress=pyrogram_progress
        )
        
        if not downloaded_file:
            raise RuntimeError("Failed to download file from Telegram.")
            
        if task.cancel_event.is_set():
            cleanup_files(input_path)
            return

        # 2. Process
        output_path = ""
        
        def processing_progress(current: float, total: float) -> None:
            if task.cancel_event.is_set():
                raise asyncio.CancelledError("Task cancelled by user.")
            percent = (current / total * 100) if total > 0 else 0
            if getattr(processing_progress, 'last_percent', 0) + 5 <= percent:
                processing_progress.last_percent = percent
                from utils import progress_bar
                bar = progress_bar(percent)
                unit_label = "steps" if task_action == "Upscaling" else "frames"
                asyncio.create_task(safe_edit_message_text(
                    app,
                    chat_id=task.progress_chat_id,
                    message_id=task.progress_message_id,
                    text=f"⚙️ **{task_action}...**\n\n{bar}\n📊 Progress: `{percent:.1f}%` ({int(current)}/{int(total)} {unit_label})",
                    parse_mode=enums.ParseMode.MARKDOWN
                ))
        processing_progress.last_percent = 0
        
        if task.task_type == TaskType.ENCODE:
            task_action = "Encoding"
            encode_settings = EncodeSettings(
                codec=task.settings.get("codec", "hevc"),
                quality=task.settings.get("quality", "medium"),
                preset=task.settings.get("preset", "medium"),
                audio_codec=task.settings.get("audio", "copy"),
                use_gpu=Config.GPU_ENABLED,
                gpu_id=gpu_id,
            )
            output_path = await encoder.encode(input_path, encode_settings, processing_progress)
            
        elif task.task_type == TaskType.UPSCALE:
            task_action = "Upscaling"
            await safe_edit_message_text(
                app,
                chat_id=task.progress_chat_id,
                message_id=task.progress_message_id,
                text="🎞️ **Preparing GPU upscaling...**",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            output_path = await upscaler.upscale(
                input_path, 
                target_resolution=task.settings.get("resolution", "4k"), 
                progress_callback=processing_progress,
                gpu_id=gpu_id,
            )
            
        elif task.task_type == TaskType.UPSCALE_ENCODE:
            task_action = "Upscaling"
            await safe_edit_message_text(
                app,
                chat_id=task.progress_chat_id,
                message_id=task.progress_message_id,
                text="🎞️ **Preparing GPU upscaling...**",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            upscaled_path = await upscaler.upscale(
                input_path, 
                target_resolution=task.settings.get("resolution", "4k"), 
                progress_callback=processing_progress,
                gpu_id=gpu_id,
            )
            if task.cancel_event.is_set():
                cleanup_files(input_path, upscaled_path)
                return
            
            task_action = "Encoding"
            processing_progress.last_percent = 0
            encode_settings = EncodeSettings(
                codec=task.settings.get("codec", "hevc"),
                quality=task.settings.get("quality", "medium"),
                preset=task.settings.get("preset", "medium"),
                audio_codec=task.settings.get("audio", "copy"),
                use_gpu=Config.GPU_ENABLED,
                gpu_id=gpu_id,
            )
            output_path = await encoder.encode(upscaled_path, encode_settings, processing_progress)
            cleanup_files(upscaled_path)

        if task.cancel_event.is_set():
            cleanup_files(input_path, output_path)
            return

        # 3. Upload
        task_action = "Uploading"
        file_size = os.path.getsize(output_path)
        tracker = ProgressTracker(file_size)

        if file_size <= Config.TG_UPLOAD_LIMIT:
            try:
                await app.send_document(
                    chat_id=task.progress_chat_id,
                    document=output_path,
                    caption=f"✅ **Done!**\n\n📁 `{os.path.basename(output_path)}`\n💾 `{human_size(file_size)}`",
                    progress=pyrogram_progress,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error("Telegram upload failed: %s", e)
                await upload_gdrive(task, output_path, file_size)
        else:
            await upload_gdrive(task, output_path, file_size)

        # 4. Cleanup
        cleanup_files(input_path, output_path)

    except asyncio.CancelledError:
        logger.info("Task %s cancelled during processing.", task.task_id)
        raise
    except Exception as e:
        logger.error("Error processing task %s: %s", task.task_id, str(e), exc_info=True)
        try:
            await safe_edit_message_text(
                app,
                chat_id=task.progress_chat_id,
                message_id=task.progress_message_id,
                text=f"❌ **Failed**\n\nError: `{str(e)[:200]}`",
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception:
            pass
        raise e

async def upload_gdrive(task: Task, output_path: str, file_size: int) -> None:
    if not gdrive.is_configured():
        await app.send_message(
            chat_id=task.progress_chat_id,
            text=f"❌ File too large ({human_size(file_size)}) and GDrive not configured.",
        )
        return

    try:
        def gdrive_progress(current: int, total: int):
            # Sync to async bridge for progress updates would go here, omitting for brevity
            pass
        
        await safe_edit_message_text(
            app,
            chat_id=task.progress_chat_id,
            message_id=task.progress_message_id,
            text=f"📤 **Uploading to Google Drive...**\n\n💾 `{human_size(file_size)}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        result = await gdrive.upload(file_path=output_path, filename=os.path.basename(output_path), progress_callback=gdrive_progress)
        
        await app.send_message(
            chat_id=task.progress_chat_id,
            text=f"✅ **Uploaded to Google Drive!**\n\n"
                 f"💾 `{human_size(file_size)}`\n\n"
                 f"📁 {result['link']}",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await app.send_message(chat_id=task.progress_chat_id, text=f"❌ GDrive upload failed: `{str(e)[:200]}`")

# ── Handlers ──────────────────────────────────────────────────────────

@app.on_message((filters.video | filters.document) & filters.private)
async def handle_video(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    
    if await db.is_banned(user_id):
        await message.reply_text("🚫 You are banned.")
        return

    file = message.video or message.document
    if not file:
        return
        
    filename = getattr(file, "file_name", "video.mp4") or "video.mp4"
    if not is_supported_video(filename):
        await message.reply_text(f"⚠️ Unsupported format: `{filename}`", parse_mode=enums.ParseMode.MARKDOWN)
        return

    file_size = file.file_size or 0
    if file_size > Config.MAX_FILE_SIZE:
        await message.reply_text(f"⚠️ File too large: `{human_size(file_size)}`")
        return

    wf = get_workflow(user_id)
    if wf and wf.get("awaiting_file"):
        # We have an active workflow waiting for this file
        wf["file_id"] = file.file_id
        wf["file_name"] = filename
        wf["file_size"] = file_size
        wf["message_id"] = message.id
        wf["awaiting_file"] = False
        
        task_type_map = {
            "encode": TaskType.ENCODE,
            "upscale": TaskType.UPSCALE,
            "upscale_encode": TaskType.UPSCALE_ENCODE
        }
        
        task_type_str = wf.get("type", "encode")
        t_type = task_type_map.get(task_type_str, TaskType.ENCODE)
        
        # Determine priority (admin = 10, normal = 0)
        priority = 10 if user_id in Config.ADMIN_IDS else 0
        
        progress_msg = await message.reply_text("⏳ **Queuing task...**", parse_mode=enums.ParseMode.MARKDOWN)
        
        task = Task(
            task_id=Task.generate_id(),
            user_id=user_id,
            task_type=t_type,
            input_file="", # Will be set during download
            settings=wf,
            priority=priority,
            progress_message_id=progress_msg.id,
            progress_chat_id=message.chat.id
        )
        
        await queue_manager.add_task(task)
        clear_workflow(user_id)
        
        position = await db.get_queue_position(task.task_id)
        if position > 1:
            await safe_edit_text(progress_msg, f"⏳ **Queued** (Position #{position})")
            
    else:
        # Quick actions
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 Upscale", callback_data="quick_upscale"),
                InlineKeyboardButton("🎬 Encode", callback_data="quick_encode"),
            ],
            [
                InlineKeyboardButton("🔍🎬 Both", callback_data="quick_both"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow"),
            ]
        ])
        
        set_workflow(user_id, {
            "file_id": file.file_id,
            "file_name": filename,
            "file_size": file_size,
            "message_id": message.id
        })
        
        await message.reply_text(
            f"📹 **Video received**\nSize: {human_size(file_size)}\n\nWhat would you like to do?",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

# Quick action callbacks
@app.on_callback_query(filters.regex(r"^quick_"))
async def on_quick_action(client: Client, query: CallbackQuery) -> None:
    user_id = query.from_user.id
    action = query.data.replace("quick_", "")
    
    wf = get_workflow(user_id)
    if not wf:
        await query.answer("⚠️ Session expired. Please send the video again.", show_alert=True)
        return
        
    if action == "upscale":
        wf["type"] = "upscale"
        set_workflow(user_id, wf)
        await safe_edit_text(query.message, "🔍 **AI Anime Upscaling**\nChoose target resolution:", reply_markup=resolution_keyboard())
    elif action == "encode":
        wf["type"] = "encode"
        set_workflow(user_id, wf)
        await safe_edit_text(query.message, "🎬 **Video Encoding**\nChoose your codec:", reply_markup=codec_keyboard())
    elif action == "both":
        wf["type"] = "upscale_encode"
        set_workflow(user_id, wf)
        await safe_edit_text(query.message, "🔍🎬 **Upscale + Encode**\nChoose target resolution:", reply_markup=resolution_keyboard())
    
    await query.answer()

# Override the specific callback step that triggers the task since we already have the file
@app.on_callback_query(filters.regex(r"^(audio_|res_)"))
async def intercept_final_step(client: Client, query: CallbackQuery) -> None:
    user_id = query.from_user.id
    wf = get_workflow(user_id)
    
    if not wf:
        # Fall back to original callbacks if no workflow
        query.continue_propagation()
        return
        
    # If we already have the file details, we shouldn't ask for it again
    if "file_id" in wf and not wf.get("awaiting_file"):
        data = query.data
        if data.startswith("audio_"):
            wf["audio"] = data.replace("audio_", "")
        elif data.startswith("res_"):
            wf["resolution"] = data.replace("res_", "")
            if wf.get("type") == "upscale_encode" and "codec" not in wf:
                # We need codec for upscale_encode
                set_workflow(user_id, wf)
                await safe_edit_text(query.message, "🎬 Choose your codec:", reply_markup=codec_keyboard())
                await query.answer()
                return

        task_type_map = {
            "encode": TaskType.ENCODE,
            "upscale": TaskType.UPSCALE,
            "upscale_encode": TaskType.UPSCALE_ENCODE
        }
        t_type = task_type_map.get(wf.get("type", "encode"), TaskType.ENCODE)
        priority = 10 if user_id in Config.ADMIN_IDS else 0
        
        await safe_edit_text(query.message, "⏳ **Queuing task...**", parse_mode=enums.ParseMode.MARKDOWN)
        
        task = Task(
            task_id=Task.generate_id(),
            user_id=user_id,
            task_type=t_type,
            input_file="",
            settings=wf,
            priority=priority,
            progress_message_id=query.message.id,
            progress_chat_id=query.message.chat.id
        )
        
        await queue_manager.add_task(task)
        clear_workflow(user_id)
        
        position = await db.get_queue_position(task.task_id)
        if position > 1:
            await safe_edit_text(query.message, f"⏳ **Queued** (Position #{position})")
            
        await query.answer("Task started!")
    else:
        # We don't have the file yet, let the original callback handle it
        query.continue_propagation()


# ── Startup ───────────────────────────────────────────────────────────

async def on_startup() -> None:
    """Initialize services."""
    logger.info("=" * 60)
    logger.info("AnimeEncoderBot starting up (Pyrogram MTProto)...")
    logger.info("=" * 60)

    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    Config.ensure_dirs()

    try:
        await db.connect()
    except Exception as e:
        logger.warning("MongoDB failed: %s", e)

    await encoder.initialize()
    logger.info("GPU: %s | HEVC: %s | AV1: %s",
                encoder.gpu_name, encoder.gpu_available, encoder.has_av1_nvenc)

    await upscaler.check_available()
    logger.info("Real-CUGAN: %s", "available" if upscaler._available else "NOT FOUND")

    logger.info("GDrive: %s", "configured" if gdrive.is_configured() else "not configured")

    queue_manager.set_processor(process_task)
    await queue_manager.start(num_workers=Config.CONCURRENT_TASKS)

    for admin_id in Config.ADMIN_IDS:
        try:
            await app.send_message(
                admin_id,
                f"🟢 **Bot Online!** (Pyrogram MTProto)\n\n"
                f"🖥 GPU: {encoder.gpu_name if encoder.gpu_available else 'CPU'}\n"
                f"📹 Send a video to start!",
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception:
            pass

    logger.info("Bot is ready!")

async def on_shutdown() -> None:
    logger.info("Shutting down...")
    await queue_manager.stop()
    await db.close()

def main() -> None:
    register_commands(app)
    register_callbacks(app)
    
    app.start()
    asyncio.get_event_loop().run_until_complete(on_startup())
    try:
        from pyrogram import idle
        idle()
    finally:
        asyncio.get_event_loop().run_until_complete(on_shutdown())
        app.stop()

if __name__ == "__main__":
    main()
