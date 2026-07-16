"""Core prodleniya (renewal) business logic — shared by:

  * group text handler (fresh requests coming from a chat message)
  * background retry worker (retries for management-API transient failures)

Two backends are consulted **in parallel** per fiscal:

  1. Cazad (api.epos.uz) — legacy, business record with `blocked_date`.
  2. Management (api.management.epos.uz) — cashdesk record with
     `licence.blockDate`.

Semantics:

  * If neither backend knows the fiscal → **NOT_FOUND**.
  * If the target date is already ≤ current backend date → **SKIPPED**
    (no rollback, we only extend forward).
  * If the management side errors with a *transient* failure
    (timeout / 5xx / network), the entry is enqueued to
    `prodleniya_pending` for later retry, and the caller is told
    to ping tech-support in-chat.
  * Cazad-side errors are surfaced immediately (they're rare per
    ops observation).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from loader import db
from utils.epos_api import EposAPIError, epos_api
from utils.epos_management_api import (
    EposMgmtAPIError,
    EposMgmtCashdeskNotFound,
    epos_mgmt_api,
)


# --- helpers ---------------------------------------------------------


def _iso_to_date(s: Optional[str]) -> Optional[date]:
    """Accepts `YYYY-MM-DD` or ISO datetime `YYYY-MM-DDTHH:...` — returns date
    or None on any parse failure."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _is_transient_mgmt_error(exc: BaseException) -> bool:
    """Everything that's NOT a clean 'cashdesk not found' from management
    is treated as transient — 5xx, network errors, timeouts and unexpected
    payloads all end up in the retry queue.

    Rationale: management site freezes intermittently (per ops), and
    retries every ~2 min recover on their own once it's back.
    """
    if isinstance(exc, EposMgmtCashdeskNotFound):
        return False
    if isinstance(exc, EposMgmtAPIError):
        return True
    # asyncio timeouts, aiohttp errors, generic OS errors — all transient.
    return isinstance(exc, (asyncio.TimeoutError, OSError))


# --- data model ------------------------------------------------------


@dataclass
class ProdOutcome:
    """Outcome of processing a single fiscal (which may exist in one, both
    or neither backend)."""

    fiscal: str
    target_iso: str

    # Cazad (api.epos.uz)
    cazad_updated: bool = False
    cazad_skipped_reason: Optional[str] = None   # 'not_found' | 'no_op' | 'error'
    cazad_error: Optional[str] = None
    cazad_current_iso: Optional[str] = None
    cazad_business_id: Optional[int] = None

    # Management (api.management.epos.uz)
    mgmt_updated: bool = False
    mgmt_skipped_reason: Optional[str] = None    # 'not_found' | 'no_op' | 'error' | 'queued'
    mgmt_error: Optional[str] = None
    mgmt_current_iso: Optional[str] = None
    mgmt_queued_id: Optional[int] = None         # queue row id when transient

    # Business info from whichever backend found the fiscal. Filled from
    # cazad first (source of truth for TIN/name in prodleniya notifications);
    # fallback to management (`businessID.tin` / `businessID.name`) if cazad
    # missed it.
    business_tin: Optional[str] = None
    business_name: Optional[str] = None

    @property
    def anything_updated(self) -> bool:
        return self.cazad_updated or self.mgmt_updated


# --- cazad side ------------------------------------------------------


async def _apply_cazad(
    fiscal: str,
    target_iso: str,
    outcome: ProdOutcome,
) -> None:
    """Query cazad + update `blocked_date` if target > current. Writes to
    `outcome` in place.

    NOTE: no diller-ownership check here — prodleniya is unrestricted by
    design (that check lives only in the PDF flow, `_auto_apply_changes`).
    """
    from handlers.users.business import (
        UPDATABLE_FIELDS,
        _flatten_fk,
        _pick_business,
        update_business,
    )
    from handlers.users.find_business import get_business_by_name

    try:
        token = await epos_api.get_token()
    except EposAPIError as e:
        outcome.cazad_skipped_reason = "error"
        outcome.cazad_error = f"get_token: {e}"
        return

    try:
        response = await get_business_by_name(fiscal, token)
    except EposAPIError as e:
        outcome.cazad_skipped_reason = "error"
        outcome.cazad_error = str(e)
        return

    business = _pick_business(response) if response is not None else {}
    business_id = business.get("id") or business.get("business_id")
    if not business_id:
        outcome.cazad_skipped_reason = "not_found"
        return

    outcome.cazad_business_id = business_id

    # Cazad: TIN / name — берём как есть, а name предпочитаем из первого
    # branch (что показывается пользователю), иначе fallback к business.name.
    outcome.business_tin = business.get("TIN") or business.get("tin")
    branches = business.get("branches") or []
    branch_name = None
    for b in branches:
        if isinstance(b, dict) and b.get("name"):
            branch_name = b["name"]
            break
    outcome.business_name = branch_name or business.get("name")

    current_iso = business.get("blocked_date")
    outcome.cazad_current_iso = current_iso
    target_d = _iso_to_date(target_iso)
    current_d = _iso_to_date(current_iso)
    if target_d and current_d and target_d <= current_d:
        outcome.cazad_skipped_reason = "no_op"
        return

    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["blocked_date"] = target_iso

    try:
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        outcome.cazad_skipped_reason = "error"
        outcome.cazad_error = str(e)
        return

    outcome.cazad_updated = True


# --- management side -------------------------------------------------


async def _apply_management(
    fiscal: str,
    target_iso: str,
    outcome: ProdOutcome,
    *,
    enqueue_context: Optional[dict] = None,
) -> None:
    """Query management + PATCH blockDate if target > current. On transient
    failure the request is enqueued to `prodleniya_pending` and outcome is
    marked as `queued` (only when `enqueue_context` is provided — retries
    from the worker don't re-enqueue)."""
    try:
        doc = await epos_mgmt_api.get_cashdesk_by_fiscal(fiscal)
    except EposMgmtCashdeskNotFound:
        outcome.mgmt_skipped_reason = "not_found"
        return
    except (EposMgmtAPIError, asyncio.TimeoutError, OSError) as e:
        # Transient — queue if this is a fresh request (not a retry).
        outcome.mgmt_error = str(e)
        if enqueue_context is not None and _is_transient_mgmt_error(e):
            try:
                qid = await db.enqueue_prodleniya(
                    chat_id=enqueue_context["chat_id"],
                    message_id=enqueue_context["message_id"],
                    user_id=enqueue_context["user_id"],
                    diller_id=enqueue_context["diller_id"],
                    fiscal_id=fiscal,
                    target_iso=target_iso,
                    error=str(e),
                )
                outcome.mgmt_queued_id = qid
                outcome.mgmt_skipped_reason = "queued"
                logging.warning(
                    "prodleniya: mgmt transient for %s, queued as id=%s: %s",
                    fiscal, qid, e,
                )
                return
            except Exception as enq_exc:
                logging.exception(
                    "prodleniya: enqueue failed for %s: %s", fiscal, enq_exc
                )
        outcome.mgmt_skipped_reason = "error"
        return

    licence = doc.get("licence") or {}
    current_iso = licence.get("blockDate")
    outcome.mgmt_current_iso = current_iso

    # Fallback: если cazad не заполнил tin/name — берём из management.
    biz = doc.get("businessID") or {}
    if not outcome.business_tin:
        outcome.business_tin = biz.get("tin") or biz.get("pinfl")
    if not outcome.business_name:
        outcome.business_name = biz.get("name")

    target_d = _iso_to_date(target_iso)
    current_d = _iso_to_date(current_iso)
    if target_d and current_d and target_d <= current_d:
        outcome.mgmt_skipped_reason = "no_op"
        return

    try:
        await epos_mgmt_api.update_cashdesk_block_date(
            cashdesk_id=doc["_id"],
            block_date=target_iso,
        )
    except (EposMgmtAPIError, asyncio.TimeoutError, OSError) as e:
        outcome.mgmt_error = str(e)
        if enqueue_context is not None and _is_transient_mgmt_error(e):
            try:
                qid = await db.enqueue_prodleniya(
                    chat_id=enqueue_context["chat_id"],
                    message_id=enqueue_context["message_id"],
                    user_id=enqueue_context["user_id"],
                    diller_id=enqueue_context["diller_id"],
                    fiscal_id=fiscal,
                    target_iso=target_iso,
                    error=str(e),
                )
                outcome.mgmt_queued_id = qid
                outcome.mgmt_skipped_reason = "queued"
                return
            except Exception as enq_exc:
                logging.exception(
                    "prodleniya: enqueue after PATCH-fail for %s: %s",
                    fiscal, enq_exc,
                )
        outcome.mgmt_skipped_reason = "error"
        return

    outcome.mgmt_updated = True


# --- public entrypoints ---------------------------------------------


async def process_fiscal(
    fiscal: str,
    target_iso: str,
    *,
    enqueue_context: Optional[dict] = None,
) -> ProdOutcome:
    """Process one fiscal against both backends in parallel."""
    outcome = ProdOutcome(fiscal=fiscal, target_iso=target_iso)
    await asyncio.gather(
        _apply_cazad(fiscal, target_iso, outcome),
        _apply_management(
            fiscal, target_iso, outcome, enqueue_context=enqueue_context
        ),
        return_exceptions=False,
    )
    return outcome


async def retry_pending_once(bot) -> None:
    """One tick of the retry worker: pull ready-to-retry rows and attempt
    the management-side update again. Called on a schedule from `app.py`.

    Success — post a follow-up reply to the original chat/message.
    Persistent failure (>= MAX_ATTEMPTS) — mark giveup and ping admin.
    """
    from utils.notify_admins import notify_admins  # local, avoids cycle

    MAX_ATTEMPTS = 180  # 180 * 2min ≈ 6h
    RETRY_INTERVAL_SEC = 120

    rows = await db.list_pending_prodleniya(limit=25)
    if not rows:
        return

    for row in rows:
        rid = row["id"]
        fiscal = row["fiscal_id"]
        target_iso = row["target_iso"].isoformat()
        try:
            doc = await epos_mgmt_api.get_cashdesk_by_fiscal(fiscal)
        except EposMgmtCashdeskNotFound:
            # Fiscal никогда не появится — не имеет смысла ждать.
            await db.mark_prodleniya_giveup(
                rid, f"NOT_FOUND on retry: {fiscal}"
            )
            try:
                await bot.send_message(
                    row["chat_id"],
                    f"❌ Отложенное продление: <code>{fiscal}</code> — "
                    f"в management не найден (после ретрая). Проверьте "
                    f"фискальный номер.",
                    reply_to_message_id=row["message_id"],
                )
            except Exception:
                pass
            continue
        except (EposMgmtAPIError, asyncio.TimeoutError, OSError) as e:
            await _reschedule_or_giveup(row, str(e), MAX_ATTEMPTS, RETRY_INTERVAL_SEC, bot)
            continue

        # Found. Enforce no-rollback again.
        licence = doc.get("licence") or {}
        current_iso = licence.get("blockDate")
        target_d = _iso_to_date(target_iso)
        current_d = _iso_to_date(current_iso)
        if target_d and current_d and target_d <= current_d:
            await db.mark_prodleniya_done(rid)
            try:
                await bot.send_message(
                    row["chat_id"],
                    f"ℹ️ Отложенное продление: <code>{fiscal}</code> — "
                    f"management-дата уже <code>{str(current_iso)[:10]}</code> "
                    f"≥ целевой <code>{target_iso}</code>, изменять не нужно.",
                    reply_to_message_id=row["message_id"],
                )
            except Exception:
                pass
            continue

        try:
            await epos_mgmt_api.update_cashdesk_block_date(
                cashdesk_id=doc["_id"],
                block_date=target_iso,
            )
        except (EposMgmtAPIError, asyncio.TimeoutError, OSError) as e:
            await _reschedule_or_giveup(row, str(e), MAX_ATTEMPTS, RETRY_INTERVAL_SEC, bot)
            continue

        await db.mark_prodleniya_done(rid)
        try:
            await bot.send_message(
                row["chat_id"],
                f"✅ Отложенное продление применено: "
                f"<code>{fiscal}</code> → <code>{target_iso}</code> "
                f"(management)",
                reply_to_message_id=row["message_id"],
            )
        except Exception:
            pass

        # Notify per-diller log chats + central PDF_GROUP_CHAT_ID — same
        # format as the synchronous path in handle_group_prodleniya.
        diller_id = row.get("diller_id")
        if diller_id is not None:
            import html as _html
            biz = doc.get("businessID") or {}
            tin = biz.get("tin") or biz.get("pinfl") or "—"
            biz_name = biz.get("name") or "—"

            # Пытаемся достать имя отправителя из Telegram (было
            # сохранено только user_id при постановке в очередь).
            sender_name = "—"
            try:
                if row.get("user_id"):
                    member = await bot.get_chat_member(
                        row["chat_id"], row["user_id"]
                    )
                    sender_name = member.user.full_name or "—"
            except Exception:
                pass

            try:
                from utils.diller import get_user_diller_name
                diller_name = (
                    await get_user_diller_name(int(row["user_id"]))
                    if row.get("user_id") else None
                ) or "—"
            except Exception:
                diller_name = "—"

            try:
                from utils.notify_groups import notify_log_groups
                summary = (
                    "🔒 <b>Обновлена дата блокировки</b>\n"
                    f"<b>Diller:</b> {_html.escape(str(diller_name))}\n"
                    f"<b>От:</b> {_html.escape(sender_name)} "
                    f"(id: <code>{row['user_id']}</code>)\n\n"
                    f"<b>Фискальный номер:</b> <code>{fiscal}</code>\n"
                    f"<b>ИНН:</b> <code>{_html.escape(str(tin))}</code>\n"
                    f"<b>Название бизнеса:</b> "
                    f"{_html.escape(str(biz_name))}\n"
                    f"<b>Дата блокировки:</b> <code>{target_iso}</code>"
                )
                await notify_log_groups(int(diller_id), summary)
            except Exception:
                logging.exception(
                    "retry_worker: notify_log_groups failed for %s", fiscal
                )


async def _reschedule_or_giveup(row, err: str, max_attempts: int, interval_sec: int, bot):
    rid = row["id"]
    attempts = int(row["attempts"] or 0) + 1
    if attempts >= max_attempts:
        await db.mark_prodleniya_giveup(rid, err)
        try:
            from utils.notify_admins import notify_admins
            await notify_admins(
                f"⚠️ Prodleniya giveup: fiscal=<code>{row['fiscal_id']}</code> "
                f"после {attempts} попыток. Ошибка: {err[:200]}"
            )
        except Exception:
            pass
        return
    next_try_at = datetime.utcnow() + timedelta(seconds=interval_sec)
    await db.reschedule_prodleniya(rid, next_try_at, err)


async def retry_worker(bot, *, interval_sec: int = 120):
    """Loop task started in on_startup. Wakes every `interval_sec` and
    processes ready rows. Never crashes the loop."""
    logging.info("prodleniya retry worker started (interval=%ss)", interval_sec)
    while True:
        try:
            await retry_pending_once(bot)
        except Exception:
            logging.exception("prodleniya retry_worker tick failed")
        await asyncio.sleep(interval_sec)
