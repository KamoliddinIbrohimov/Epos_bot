"""Helpers for routing event notifications.

A 'log' chat is a Telegram group/supergroup that has been linked to a
specific diller via the chat-approval flow (see
`handlers/groups/new_chat.py`). When something happens to a business
belonging to that diller (block_date update, new client registration,
fiscal module change, address change), we forward an info message to:

  1. every per-diller log chat bound to *that* diller, and
  2. additionally the global PDF_GROUP_CHAT_ID from .env, if set, so
     that admins watching the central group see every dealer's
     activity in one place.

We de-duplicate: if PDF_GROUP_CHAT_ID happens to also be in the
per-diller list (or another diller's same group_type='log' chat), it
receives exactly one copy.
"""

import logging
from typing import Optional

from data import config
from loader import bot, db


def _recipient_chat_ids(per_diller_chat_ids):
    """Combine per-diller log chats with the global PDF_GROUP_CHAT_ID
    (if set), preserving order and dropping duplicates."""
    out = []
    seen = set()
    for cid in per_diller_chat_ids:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    central = config.PDF_GROUP_CHAT_ID
    if central and central not in seen:
        out.append(central)
        seen.add(central)
    return out


async def notify_log_groups(
    diller_id: Optional[int],
    text: str,
    *,
    disable_web_page_preview: bool = True,
) -> None:
    """Send a plain HTML message to every log-chat linked to `diller_id`,
    plus the global PDF_GROUP_CHAT_ID (if configured).

    Per-chat failures are logged but don't propagate.
    """
    per_diller = []
    if diller_id is not None:
        per_diller = await db.get_log_chats_for_diller(int(diller_id))

    for chat_id in _recipient_chat_ids(per_diller):
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
    """Send a document (PDF) with caption to every log-chat for the diller,
    plus the global PDF_GROUP_CHAT_ID (if configured)."""
    per_diller = []
    if diller_id is not None:
        per_diller = await db.get_log_chats_for_diller(int(diller_id))

    for chat_id in _recipient_chat_ids(per_diller):
        try:
            await bot.send_document(
                chat_id=chat_id, document=doc_file_id, caption=caption
            )
        except Exception as e:
            logging.exception(
                f"notify_log_groups_doc: failed to send to chat {chat_id}: {e}"
            )
