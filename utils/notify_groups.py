"""Helpers for routing event notifications to per-diller log groups.

A 'log' chat is a Telegram group/supergroup that has been linked to a
specific diller via the chat-approval flow (see
`handlers/groups/new_chat.py`). When some event happens to a business
belonging to that diller (block_date update, new client registration,
fiscal module change, address change), we forward an info message to
every such log chat — and ONLY those bound to this specific diller.

`PDF_GROUP_CHAT_ID` from .env is no longer used by this helper; per-
diller routing replaces the single global notify group for these
events.
"""

import logging
from typing import Optional

from loader import bot, db


async def notify_log_groups(
    diller_id: Optional[int],
    text: str,
    *,
    disable_web_page_preview: bool = True,
) -> None:
    """Send a plain HTML message to every log-chat linked to `diller_id`.

    Silently no-ops if diller_id is None or if no log chats are bound.
    Per-chat failures are logged but don't propagate.
    """
    if diller_id is None:
        return

    chat_ids = await db.get_log_chats_for_diller(int(diller_id))
    if not chat_ids:
        return

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id,
                text,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as e:
            logging.exception(
                f"notify_log_groups: failed to send to chat {chat_id}: {e}"
            )


async def notify_log_groups_doc(
    diller_id: Optional[int],
    doc_file_id: str,
    caption: str,
) -> None:
    """Send a document (PDF) with caption to every log-chat for the diller."""
    if diller_id is None:
        return

    chat_ids = await db.get_log_chats_for_diller(int(diller_id))
    if not chat_ids:
        return

    for chat_id in chat_ids:
        try:
            await bot.send_document(
                chat_id=chat_id, document=doc_file_id, caption=caption
            )
        except Exception as e:
            logging.exception(
                f"notify_log_groups_doc: failed to send to chat {chat_id}: {e}"
            )
