"""
Bot command handlers for AnimeEncoderBot.
All /slash commands are defined and registered here.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from callbacks import (
    clear_workflow,
    codec_keyboard,
    resolution_keyboard,
    set_workflow,
    _settings_keyboard,
)
from config import Config
from database import db
from encoder import encoder
from queue_manager import queue_manager
from upscaler import upscaler
from utils import format_duration, human_size

logger = logging.getLogger(__name__)


# ── Decorators ────────────────────────────────────────────────────────

def admin_only(func):
    """Decorator to restrict a command to admin users."""
    async def wrapper(client: Client, message: Message):
        if message.from_user.id not in Config.ADMIN_IDS:
            await message.reply_text("🚫 This command is for admins only.")
            return
        return await func(client, message)
    return wrapper


# ── Command Registration ─────────────────────────────────────────────

def register_commands(app: Client) -> None:
    """Register all command handlers on the Pyrogram client."""

    # ── Welcome images ──
    WELCOME_IMAGES_DIR = Path(__file__).parent / "assets"

    def _get_random_welcome_image() -> str:
        """Pick a random welcome image from assets/."""
        images = sorted(WELCOME_IMAGES_DIR.glob("welcome_*.png"))
        if not images:
            return ""
        return str(random.choice(images))

    # ── /start ──

    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message) -> None:
        """Welcome message with anime waifu image."""
        user = message.from_user
        await db.add_user(user.id, user.username or "")

        gpu_status = f"✅ {encoder.gpu_name}" if encoder.gpu_available else "❌ CPU mode"
        upscaler_status = "✅ Available" if await upscaler.check_available() else "❌ Not installed"

        welcome_text = (
            f"👋 **Hello {user.first_name}!**\n\n"
            f"🎬 I am **AnimeEncoderBot**\n"
            f"_Professional AI-Enhanced Video Encoding._\n\n"
            f"> 🧠 AI Upscaling: Real-ESRGAN (Anime V3)\n"
            f"> 🎬 Codecs: H.265 (HEVC) / AV1\n"
            f"> 📺 Resolution: Up to 8K\n"
            f"> ⚡ GPU Accelerated: {gpu_status}\n"
            f"> 🔍 Upscaler: {upscaler_status}\n\n"
            f"**📋 Quick Start**\n"
            f"├ /encode — Encode video (AV1 / HEVC)\n"
            f"├ /upscale — AI upscale anime video\n"
            f"├ /help — Full command list\n"
            f"└ /settings — Your preferences"
        )

        # Send with random anime waifu image
        welcome_img = _get_random_welcome_image()
        if welcome_img:
            try:
                await message.reply_photo(
                    photo=welcome_img,
                    caption=welcome_text,
                )
                return
            except Exception as e:
                logger.warning("Failed to send welcome image: %s", e)

        # Fallback to text-only
        await message.reply_text(welcome_text)

    # ── /help ──

    @app.on_message(filters.command("help") & filters.private)
    async def cmd_help(client: Client, message: Message) -> None:
        """Detailed help."""
        await message.reply_text(
            "📖 **AnimeEncoderBot — Help**\n\n"
            "**Encoding Commands**\n"
            "├ /encode — Start encoding workflow\n"
            "│   Choose codec → quality → preset → audio → send video\n"
            "│   Codecs: **AV1** (SVT-AV1) or **HEVC** (H.265 NVENC)\n"
            "│\n"
            "**Upscaling Commands**\n"
            "├ /upscale — AI upscale (anime optimized)\n"
            "│   Choose resolution → send video\n"
            "│   Targets: 1080p, 2K, 4K, 8K\n"
            "│   Model: Real-ESRGAN Anime V3\n"
            "│\n"
            "**General Commands**\n"
            "├ /status — Check your current task\n"
            "├ /cancel — Cancel your active task\n"
            "├ /queue — View the task queue\n"
            "├ /settings — Your default preferences\n"
            "├ /help — This message\n"
            "│\n"
            "**Admin Commands**\n"
            "├ /stats — Bot statistics\n"
            "├ /broadcast `<msg>` — Message all users\n"
            "├ /ban `<user_id>` — Ban a user\n"
            "├ /unban `<user_id>` — Unban a user\n"
            "└ /logs — Recent task logs\n\n"
            "**How it works:**\n"
            "1️⃣ Use /encode or /upscale to configure settings\n"
            "2️⃣ Send your video file\n"
            "3️⃣ Wait for processing (you'll see live progress)\n"
            "4️⃣ Receive the encoded/upscaled file\n\n"
            "**Supported formats:** MP4, MKV, AVI, MOV, WebM, FLV, WMV, M4V, TS, M2TS, VOB\n"
            f"**Max file size:** {human_size(Config.MAX_FILE_SIZE)}",
        )

    # ── /encode ──

    @app.on_message(filters.command("encode") & filters.private)
    async def cmd_encode(client: Client, message: Message) -> None:
        """Start encoding workflow."""
        user_id = message.from_user.id

        if await db.is_banned(user_id):
            await message.reply_text("🚫 You are banned.")
            return

        # Check for active task
        active = await db.get_user_active_task(user_id)
        if active:
            await message.reply_text(
                "⚠️ You already have an active task.\n"
                "Use /cancel to cancel it or /status to check progress."
            )
            return

        # Initialize workflow
        set_workflow(user_id, {"type": "encode", "awaiting_file": False})

        await message.reply_text(
            "🎬 **Video Encoding**\n\n"
            "Choose your codec:",
            reply_markup=codec_keyboard(),
        )

    # ── /upscale ──

    @app.on_message(filters.command("upscale") & filters.private)
    async def cmd_upscale(client: Client, message: Message) -> None:
        """Start upscaling workflow."""
        user_id = message.from_user.id

        if await db.is_banned(user_id):
            await message.reply_text("🚫 You are banned.")
            return

        active = await db.get_user_active_task(user_id)
        if active:
            await message.reply_text(
                "⚠️ You already have an active task.\n"
                "Use /cancel to cancel it or /status to check progress."
            )
            return

        # Check if upscaler is available
        if not await upscaler.check_available():
            await message.reply_text(
                "❌ **Upscaler Not Available**\n\n"
                "Real-ESRGAN is not installed on this server.\n"
                "Contact the admin to set it up."
            )
            return

        set_workflow(user_id, {"type": "upscale", "awaiting_file": False})

        await message.reply_text(
            "🔍 **AI Anime Upscaling**\n\n"
            "Using **Real-ESRGAN Anime V3** model.\n\n"
            "Choose target resolution:",
            reply_markup=resolution_keyboard(),
        )

    # ── /status ──

    @app.on_message(filters.command("status") & filters.private)
    async def cmd_status(client: Client, message: Message) -> None:
        """Check current task status."""
        user_id = message.from_user.id
        active = await db.get_user_active_task(user_id)

        if not active:
            await message.reply_text("📭 No active tasks. Use /encode or /upscale to start.")
            return

        status_emoji = {
            "queued": "⏳",
            "processing": "⚙️",
            "completed": "✅",
            "failed": "❌",
            "cancelled": "🚫",
            "timeout": "⏰",
        }

        status = active.get("status", "unknown")
        emoji = status_emoji.get(status, "❓")
        position = await db.get_queue_position(active["task_id"]) if status == "queued" else 0

        text = (
            f"{emoji} **Task Status**\n\n"
            f"🆔 ID: `{active['task_id']}`\n"
            f"📋 Type: **{active.get('type', 'unknown').capitalize()}**\n"
            f"📊 Status: **{status.capitalize()}**\n"
        )

        if position > 0:
            text += f"📍 Queue Position: **#{position}**\n"

        if active.get("progress", 0) > 0:
            text += f"📈 Progress: **{active['progress']:.1f}%**\n"

        if active.get("error"):
            text += f"\n⚠️ Error: `{active['error'][:200]}`\n"

        text += f"\n🕐 Created: `{active.get('created_at', 'N/A')}`"

        await message.reply_text(text)

    # ── /cancel ──

    @app.on_message(filters.command("cancel") & filters.private)
    async def cmd_cancel(client: Client, message: Message) -> None:
        """Cancel active task."""
        user_id = message.from_user.id

        # Clear workflow state
        clear_workflow(user_id)

        # Cancel active DB task
        active = await db.get_user_active_task(user_id)
        if active:
            cancelled = await queue_manager.cancel_task(active["task_id"])
            if cancelled:
                await message.reply_text(
                    f"✅ Task `{active['task_id']}` cancelled."
                )
            else:
                await message.reply_text(
                    "⚠️ Could not cancel the task (it may have already completed)."
                )
        else:
            await message.reply_text("📭 No active task to cancel.")

    # ── /settings ──

    @app.on_message(filters.command("settings") & filters.private)
    async def cmd_settings(client: Client, message: Message) -> None:
        """Show and manage user settings."""
        user_id = message.from_user.id
        user = await db.get_user(user_id)

        if not user:
            await db.add_user(user_id, message.from_user.username or "")
            user = await db.get_user(user_id)

        settings = user.get("settings", {})
        text = (
            "⚙️ **Your Settings**\n\n"
            f"🎬 Default Codec: **{settings.get('default_codec', 'hevc').upper()}**\n"
            f"✨ Default Quality: **{settings.get('default_quality', 'medium').capitalize()}**\n"
            f"🏃 Default Preset: **{settings.get('default_preset', 'medium').capitalize()}**\n\n"
            "Tap a button below to change:"
        )
        await message.reply_text(text, reply_markup=_settings_keyboard(settings))

    # ── /queue ──

    @app.on_message(filters.command("queue") & filters.private)
    async def cmd_queue(client: Client, message: Message) -> None:
        """Show the task queue."""
        info = await queue_manager.get_queue_info()

        text = (
            "📋 **Task Queue**\n\n"
            f"⏳ Queued: **{info['queued']}**\n"
            f"⚙️ Processing: **{info['processing']}**\n"
            f"👷 Workers: **{info['total_workers']}**\n"
        )

        if info["tasks"]:
            text += "\n**Upcoming tasks:**\n"
            for i, task in enumerate(info["tasks"][:10], 1):
                text += f"`{i}.` {task['type']} — User `{task['user_id']}` — `{task['task_id']}`\n"

        await message.reply_text(text)

    # ── /stats (Admin) ──

    @app.on_message(filters.command("stats") & filters.private)
    @admin_only
    async def cmd_stats(client: Client, message: Message) -> None:
        """Bot statistics (admin only)."""
        stats = await db.get_stats()
        user_count = await db.get_user_count()
        queue_info = await queue_manager.get_queue_info()

        gpu_info = f"✅ {encoder.gpu_name}" if encoder.gpu_available else "❌ CPU only"

        await message.reply_text(
            "📊 **Bot Statistics**\n\n"
            f"👥 Total Users: **{user_count}**\n"
            f"🎬 Total Encodes: **{stats.get('total_encodes', 0)}**\n"
            f"🔍 Total Upscales: **{stats.get('total_upscales', 0)}**\n"
            f"💾 Data Processed: **{human_size(stats.get('total_data_processed', 0))}**\n\n"
            f"**System**\n"
            f"🖥 GPU: {gpu_info}\n"
            f"⏳ Queued: {queue_info['queued']}\n"
            f"⚙️ Processing: {queue_info['processing']}\n"
            f"👷 Workers: {queue_info['total_workers']}",
        )

    # ── /broadcast (Admin) ──

    @app.on_message(filters.command("broadcast") & filters.private)
    @admin_only
    async def cmd_broadcast(client: Client, message: Message) -> None:
        """Broadcast a message to all users."""
        if len(message.command) < 2:
            await message.reply_text("Usage: /broadcast <message>")
            return

        text = message.text.split(None, 1)[1]
        user_ids = await db.get_all_user_ids()

        progress_msg = await message.reply_text(
            f"📡 Broadcasting to {len(user_ids)} users..."
        )

        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                await client.send_message(uid, f"📢 **Broadcast**\n\n{text}")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.1)  # Rate limit

        await progress_msg.edit_text(
            f"📡 **Broadcast Complete**\n\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}\n"
            f"📊 Total: {len(user_ids)}"
        )

    # ── /ban (Admin) ──

    @app.on_message(filters.command("ban") & filters.private)
    @admin_only
    async def cmd_ban(client: Client, message: Message) -> None:
        """Ban a user."""
        if len(message.command) < 2:
            await message.reply_text("Usage: /ban <user_id>")
            return

        try:
            target_id = int(message.command[1])
        except ValueError:
            await message.reply_text("⚠️ Invalid user ID.")
            return

        if target_id in Config.ADMIN_IDS:
            await message.reply_text("⚠️ Cannot ban an admin.")
            return

        if await db.ban_user(target_id):
            await message.reply_text(f"🚫 User `{target_id}` has been banned.")
        else:
            await message.reply_text(f"⚠️ User `{target_id}` not found.")

    # ── /unban (Admin) ──

    @app.on_message(filters.command("unban") & filters.private)
    @admin_only
    async def cmd_unban(client: Client, message: Message) -> None:
        """Unban a user."""
        if len(message.command) < 2:
            await message.reply_text("Usage: /unban <user_id>")
            return

        try:
            target_id = int(message.command[1])
        except ValueError:
            await message.reply_text("⚠️ Invalid user ID.")
            return

        if await db.unban_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been unbanned.")
        else:
            await message.reply_text(f"⚠️ User `{target_id}` not found.")

    # ── /logs (Admin) ──

    @app.on_message(filters.command("logs") & filters.private)
    @admin_only
    async def cmd_logs(client: Client, message: Message) -> None:
        """Get recent task logs."""
        tasks = await db.get_recent_tasks(limit=15)

        if not tasks:
            await message.reply_text("📭 No tasks found.")
            return

        text = "📋 **Recent Tasks**\n\n"
        for t in tasks:
            status_emoji = {
                "queued": "⏳", "processing": "⚙️", "completed": "✅",
                "failed": "❌", "cancelled": "🚫", "timeout": "⏰",
            }.get(t.get("status", ""), "❓")

            text += (
                f"{status_emoji} `{t['task_id']}` — "
                f"{t.get('type', '?')} — "
                f"User `{t['user_id']}` — "
                f"{t.get('status', '?')}\n"
            )

        await message.reply_text(text)
