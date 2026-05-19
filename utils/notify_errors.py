import html
import logging
import traceback
from typing import Optional

from data.config import ADMINS

MESSAGE_LIMIT = 3500


async def notify_admins_error(title: str, exc: Optional[BaseException] = None,
                              extra: Optional[str] = None) -> None:
    """Send an error report to every admin. Best-effort: failures are logged
    but never re-raised, to avoid recursive error storms."""
    from loader import bot

    parts = [f"⚠️ <b>{html.escape(title)}</b>"]
    if extra:
        parts.append(html.escape(extra))
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        parts.append(f"<pre>{html.escape(tb)}</pre>")

    text = "\n\n".join(parts)
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT] + "\n…[truncated]"

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e:
            logging.warning("notify_admins_error: failed to notify %s: %s",
                            admin_id, e)


async def notify_admins(text: str) -> None:
    """Plain admin notification (non-error). Best-effort."""
    from loader import bot
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e:
            logging.warning("notify_admins: failed to notify %s: %s", admin_id, e)
