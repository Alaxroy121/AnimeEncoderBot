"""Telegram API helper functions."""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _is_message_not_modified(exc: Exception) -> bool:
    """Return True for Pyrogram's MESSAGE_NOT_MODIFIED RPC error."""
    return exc.__class__.__name__ == "MessageNotModified" or "MESSAGE_NOT_MODIFIED" in str(exc)


async def safe_edit_text(message: Any, text: str, **kwargs: Any) -> Optional[Any]:
    """Edit a message while ignoring Telegram's no-op edit error.

    Telegram raises MESSAGE_NOT_MODIFIED when a user presses the same inline
    button twice or when two handlers race to set identical text/markup. That
    should not be treated as a bot failure.
    """
    try:
        return await message.edit_text(text, **kwargs)
    except Exception as exc:
        if not _is_message_not_modified(exc):
            raise
        logger.debug("Skipped no-op Telegram message edit for message %s", getattr(message, "id", None))
        return None


async def safe_edit_message_text(client: Any, **kwargs: Any) -> Optional[Any]:
    """Edit a message by chat/message id while ignoring no-op edit errors."""
    try:
        return await client.edit_message_text(**kwargs)
    except Exception as exc:
        if not _is_message_not_modified(exc):
            raise
        logger.debug(
            "Skipped no-op Telegram message edit for chat %s message %s",
            kwargs.get("chat_id"),
            kwargs.get("message_id"),
        )
        return None
