"""
Inline button callback handlers for AnimeEncoderBot.
Handles codec selection, quality presets, resolution, and confirmations.
"""

import logging
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from database import db
from telegram_helpers import safe_edit_text

logger = logging.getLogger(__name__)

# ── State tracking for multi-step workflows ──────────────────────────
# user_id -> workflow state dict
user_workflows: dict[int, dict] = {}


def get_workflow(user_id: int) -> Optional[dict]:
    """Get the current workflow state for a user."""
    return user_workflows.get(user_id)


def set_workflow(user_id: int, data: dict) -> None:
    """Set workflow state for a user."""
    user_workflows[user_id] = data


def clear_workflow(user_id: int) -> None:
    """Clear workflow state for a user."""
    user_workflows.pop(user_id, None)


# ── Keyboard Builders ────────────────────────────────────────────────

def codec_keyboard() -> InlineKeyboardMarkup:
    """Build codec selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 AV1 (SVT-AV1)", callback_data="codec_av1"),
            InlineKeyboardButton("🎬 HEVC (H.265)", callback_data="codec_hevc"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])


def quality_keyboard() -> InlineKeyboardMarkup:
    """Build quality selection keyboard."""
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
    """Build encoding preset keyboard."""
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
    """Build audio codec selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Copy (No Re-encode)", callback_data="audio_copy"),
        ],
        [
            InlineKeyboardButton("🔊 AAC 192k", callback_data="audio_aac"),
            InlineKeyboardButton("🔊 Opus 192k", callback_data="audio_opus"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
    ])


def resolution_keyboard() -> InlineKeyboardMarkup:
    """Build resolution selection keyboard for upscaling."""
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


def confirm_keyboard(task_type: str) -> InlineKeyboardMarkup:
    """Build confirmation keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Start", callback_data=f"confirm_{task_type}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow"),
        ],
    ])


# ── Callback Handler Registration ────────────────────────────────────

def register_callbacks(app: Client) -> None:
    """Register all callback query handlers."""

    @app.on_callback_query(filters.regex(r"^codec_"))
    async def on_codec_select(client: Client, query: CallbackQuery) -> None:
        """Handle codec selection."""
        user_id = query.from_user.id
        wf = get_workflow(user_id)
        if not wf or wf.get("type") not in ("encode", "upscale_encode"):
            await query.answer("⚠️ No active workflow. Use /encode or send a video first.", show_alert=True)
            return

        codec = query.data.replace("codec_", "")
        wf["codec"] = codec
        set_workflow(user_id, wf)

        prefix = ""
        if wf.get("type") == "upscale_encode":
            prefix = f"📐 Target Resolution: **{wf.get('resolution', '').upper()}**\n"

        await safe_edit_text(
            query.message,
            f"{prefix}🎬 Codec: **{codec.upper()}**\n\n"
            "Choose quality level:",
            reply_markup=quality_keyboard(),
        )
        await query.answer()

    @app.on_callback_query(filters.regex(r"^quality_"))
    async def on_quality_select(client: Client, query: CallbackQuery) -> None:
        """Handle quality selection."""
        user_id = query.from_user.id
        wf = get_workflow(user_id)
        if not wf or wf.get("type") not in ("encode", "upscale_encode"):
            await query.answer("⚠️ No active workflow.", show_alert=True)
            return

        quality = query.data.replace("quality_", "")
        wf["quality"] = quality
        set_workflow(user_id, wf)

        prefix = ""
        if wf.get("type") == "upscale_encode":
            prefix = f"📐 Target Resolution: **{wf.get('resolution', '').upper()}**\n"

        await safe_edit_text(
            query.message,
            f"{prefix}🎬 Codec: **{wf['codec'].upper()}**\n"
            f"✨ Quality: **{quality.capitalize()}**\n\n"
            "Choose encoding speed preset:",
            reply_markup=preset_keyboard(),
        )
        await query.answer()

    @app.on_callback_query(filters.regex(r"^preset_"))
    async def on_preset_select(client: Client, query: CallbackQuery) -> None:
        """Handle preset selection."""
        user_id = query.from_user.id
        wf = get_workflow(user_id)
        if not wf or wf.get("type") not in ("encode", "upscale_encode"):
            await query.answer("⚠️ No active workflow.", show_alert=True)
            return

        preset = query.data.replace("preset_", "")
        wf["preset"] = preset
        set_workflow(user_id, wf)

        prefix = ""
        if wf.get("type") == "upscale_encode":
            prefix = f"📐 Target Resolution: **{wf.get('resolution', '').upper()}**\n"

        await safe_edit_text(
            query.message,
            f"{prefix}🎬 Codec: **{wf['codec'].upper()}**\n"
            f"✨ Quality: **{wf['quality'].capitalize()}**\n"
            f"🏃 Preset: **{preset.capitalize()}**\n\n"
            "Choose audio handling:",
            reply_markup=audio_keyboard(),
        )
        await query.answer()

    @app.on_callback_query(filters.regex(r"^audio_"))
    async def on_audio_select(client: Client, query: CallbackQuery) -> None:
        """Handle audio codec selection — shows confirmation."""
        user_id = query.from_user.id
        wf = get_workflow(user_id)
        if not wf or wf.get("type") not in ("encode", "upscale_encode"):
            await query.answer("⚠️ No active workflow.", show_alert=True)
            return

        audio = query.data.replace("audio_", "")
        wf["audio"] = audio
        set_workflow(user_id, wf)

        if wf.get("type") == "upscale_encode":
            summary = (
                "📋 **Encoding & Upscaling Settings Summary**\n\n"
                f"📐 Target Resolution: **{wf.get('resolution', '').upper()}**\n"
                f"🎬 Codec: **{wf['codec'].upper()}**\n"
                f"✨ Quality: **{wf['quality'].capitalize()}**\n"
                f"🏃 Preset: **{wf['preset'].capitalize()}**\n"
                f"🔊 Audio: **{audio.upper()}**\n\n"
                "Send the video file to start processing, or press Cancel."
            )
        else:
            summary = (
                "📋 **Encoding Settings Summary**\n\n"
                f"🎬 Codec: **{wf['codec'].upper()}**\n"
                f"✨ Quality: **{wf['quality'].capitalize()}**\n"
                f"🏃 Preset: **{wf['preset'].capitalize()}**\n"
                f"🔊 Audio: **{audio.upper()}**\n\n"
                "Send the video file to start encoding, or press Cancel."
            )

        wf["awaiting_file"] = True
        set_workflow(user_id, wf)

        await safe_edit_text(
            query.message,
            summary,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
            ]),
        )
        await query.answer()

    @app.on_callback_query(filters.regex(r"^res_"))
    async def on_resolution_select(client: Client, query: CallbackQuery) -> None:
        """Handle resolution selection for upscaling."""
        user_id = query.from_user.id
        wf = get_workflow(user_id)
        if not wf or wf.get("type") not in ("upscale", "upscale_encode"):
            await query.answer("⚠️ No active workflow. Use /upscale first.", show_alert=True)
            return

        resolution = query.data.replace("res_", "")
        wf["resolution"] = resolution
        set_workflow(user_id, wf)

        if wf.get("type") == "upscale_encode":
            # Go to codec selection
            await safe_edit_text(
                query.message,
                "📐 Choose your codec:",
                reply_markup=codec_keyboard(),
            )
            await query.answer()
            return

        wf["awaiting_file"] = True
        set_workflow(user_id, wf)

        res_labels = {"1080p": "1920×1080", "2k": "2560×1440", "4k": "3840×2160", "8k": "7680×4320"}
        label = res_labels.get(resolution, resolution)

        await safe_edit_text(
            query.message,
            f"🔍 **Upscaling Settings**\n\n"
            f"📐 Target: **{resolution.upper()}** ({label})\n"
            f"🤖 Model: **Real-ESRGAN (Anime V3)**\n\n"
            "Send the video file to start upscaling, or press Cancel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_workflow")],
            ]),
        )
        await query.answer()

    @app.on_callback_query(filters.regex(r"^confirm_"))
    async def on_confirm(client: Client, query: CallbackQuery) -> None:
        """Handle task confirmation."""
        await query.answer("✅ Task confirmed!")

    @app.on_callback_query(filters.regex(r"^cancel_workflow$"))
    async def on_cancel_workflow(client: Client, query: CallbackQuery) -> None:
        """Handle workflow cancellation."""
        user_id = query.from_user.id
        clear_workflow(user_id)

        await safe_edit_text(query.message, "❌ Workflow cancelled.")
        await query.answer("Cancelled")

    @app.on_callback_query(filters.regex(r"^settings_"))
    async def on_settings_change(client: Client, query: CallbackQuery) -> None:
        """Handle settings changes."""
        user_id = query.from_user.id
        data = query.data.replace("settings_", "")

        if data.startswith("codec_"):
            codec = data.replace("codec_", "")
            await db.update_user_settings(user_id, {"default_codec": codec})
            await query.answer(f"Default codec set to {codec.upper()}")
        elif data.startswith("quality_"):
            quality = data.replace("quality_", "")
            await db.update_user_settings(user_id, {"default_quality": quality})
            await query.answer(f"Default quality set to {quality.capitalize()}")
        elif data.startswith("preset_"):
            preset = data.replace("preset_", "")
            await db.update_user_settings(user_id, {"default_preset": preset})
            await query.answer(f"Default preset set to {preset.capitalize()}")

        # Refresh settings display
        user = await db.get_user(user_id)
        if user:
            settings = user.get("settings", {})
            text = (
                "⚙️ **Your Settings**\n\n"
                f"🎬 Default Codec: **{settings.get('default_codec', 'hevc').upper()}**\n"
                f"✨ Default Quality: **{settings.get('default_quality', 'medium').capitalize()}**\n"
                f"🏃 Default Preset: **{settings.get('default_preset', 'medium').capitalize()}**\n"
            )
            await safe_edit_text(
                query.message,
                text,
                reply_markup=_settings_keyboard(settings),
            )


def _settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Build settings inline keyboard."""
    current_codec = settings.get("default_codec", "hevc")
    other_codec = "av1" if current_codec == "hevc" else "hevc"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Switch to {other_codec.upper()}",
                callback_data=f"settings_codec_{other_codec}",
            ),
        ],
        [
            InlineKeyboardButton("Quality: Low", callback_data="settings_quality_low"),
            InlineKeyboardButton("Quality: Med", callback_data="settings_quality_medium"),
            InlineKeyboardButton("Quality: High", callback_data="settings_quality_high"),
        ],
        [
            InlineKeyboardButton("Preset: Fast", callback_data="settings_preset_fast"),
            InlineKeyboardButton("Preset: Med", callback_data="settings_preset_medium"),
            InlineKeyboardButton("Preset: Slow", callback_data="settings_preset_slow"),
        ],
    ])
