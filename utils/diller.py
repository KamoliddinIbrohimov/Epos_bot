"""Helpers for resolving the diller (dealer) tied to a Telegram user.

Used in flows that emit messages to PDF_GROUP_CHAT_ID — the group expects every
notification to carry the diller's display name plus the sender's TG info.
"""
from typing import Optional

from loader import db


async def get_user_diller_name(user_id: int) -> Optional[str]:
    """Return the diller name linked to a Telegram user_id, or None if the
    user is not registered as any diller."""
    diller_ids = await db.get_diller_ids_by_chat_id(user_id)
    if not diller_ids:
        return None
    row = await db.get_diller(diller_ids[0])
    if row and row.get("name"):
        return row["name"]
    return None
