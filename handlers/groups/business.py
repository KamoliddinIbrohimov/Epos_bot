import html
import logging
import os
import tempfile
from typing import List, Optional, Tuple

from aiogram import types

from data import config
from handlers.users.business import (
    BRANCH_UPDATABLE_FIELDS,
    UPDATABLE_FIELDS,
    _calc_blocked_date,
    _flatten_fk,
    _pick_business,
    create_branch,
    get_business,
    update_branch,
    update_business,
)
from loader import bot, db, dp
from utils.diller import get_user_diller_name
from utils.epos_api import EposAPIError, epos_api
from utils.notify_groups import notify_log_groups
from utils.parse_pdf import PdfParseError, format_analysis, parse_business_pdf
from utils.parse_prodleniya import FISCAL_RE, parse_prodleniya_text
from utils.prodleniya_service import ProdOutcome, process_fiscal

CAPTION_LIMIT = 1024

# Username, который пингуется в группе, если во время prodleniya что-то
# не удалось (бизнес не найден в Cazad, API вернул ошибку и т.п.).
# Обычный @-mention: Telegram нотифицирует пользователя, если он состоит
# в этой группе.
TECH_SUPPORT_USERNAME = "epos_kamoliddin"


@dp.message_handler(
    chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP],
    content_types=types.ContentType.DOCUMENT,
)
async def handle_group_pdf(message: types.Message):
    doc = message.document
    if not (doc.file_name and doc.file_name.lower().endswith(".pdf")):
        return

    chat_row = await db.get_chat(message.chat.id)
    if not chat_row or chat_row["status"] != "approved":
        if chat_row and chat_row["status"] == "pending":
            await message.reply(
                "⏳ Группа ещё не одобрена администратором. PDF не обработан."
            )
        return

    # Регистрация PDF идёт ТОЛЬКО в группах типа 'registration'.
    # Лог-группы получают уведомления о событиях, но сами PDF не обрабатывают.
    if chat_row.get("group_type") != "registration":
        return

    chat_diller_id = chat_row.get("diller_id")
    if chat_diller_id is None:
        # Group is approved but not linked yet — should not happen under current
        # approval flow, but guard anyway.
        await message.reply(
            "⚠️ Группа не привязана к дилеру. PDF не обработан."
        )
        return

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        await doc.download(destination_file=tmp_path)
        try:
            parsed = parse_business_pdf(tmp_path)
        except PdfParseError as e:
            await message.reply(f"Не удалось разобрать PDF: {e}")
            return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    text = format_analysis(parsed)

    await _send_doc_with_text(message.chat.id, doc.file_id, text)

    # Notification routed to PDF group with diller name + sender info.
    if config.PDF_GROUP_CHAT_ID:
        user = message.from_user
        chat = message.chat
        name = html.escape(user.full_name)
        chat_title = html.escape(chat.title or "—")
        diller_name = await get_user_diller_name(user.id) or "—"
        group_text = (
            f"📂 PDF из группы: <b>{chat_title}</b> "
            f"(id: <code>{chat.id}</code>)\n"
            f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
            f'От: <a href="tg://user?id={user.id}">{name}</a> '
            f"(id: <code>{user.id}</code>)\n\n{text}"
        )
        try:
            await _send_doc_with_text(
                config.PDF_GROUP_CHAT_ID, doc.file_id, group_text
            )
        except Exception as e:
            logging.exception(f"failed to notify PDF group: {e}")

    # Auto-apply business changes based on parsed status.
    await _auto_apply_changes(message, parsed, chat_diller_id)


async def _auto_apply_changes(
    message: types.Message, parsed: dict, chat_diller_id: int
) -> None:
    """
    Стратегия поиска бизнеса в Cazad зависит от статуса PDF:

      * "Новый клиент"               — lookup по zavod (virtual_number)
      * "Фискальный модуль изменён"  — lookup по old_fiscal, затем new_fiscal
      * "Адрес изменён"              — lookup по текущему фискальному номеру

    Zavod НЕ используется для fiscal/address — только для new client,
    т.к. у нового бизнеса в Cazad ещё может не быть `name`.
    """
    holati = parsed.get("holati")
    fiscal_modules = parsed.get("fiskal_modules") or []

    try:
        token = await epos_api.get_token()
    except EposAPIError as e:
        logging.exception("get_token failed in group flow")
        await message.reply(f"⚠️ get_token: {html.escape(str(e))}")
        return

    # --- Новый клиент: по zavod, как раньше ---
    if holati == "Новый клиент":
        zavod = parsed.get("zavod")
        if not zavod or zavod == "—":
            return
        try:
            business_data = await get_business(zavod, token)
        except EposAPIError as e:
            logging.exception("get_business failed in group flow")
            await message.reply(f"⚠️ get_business: {html.escape(str(e))}")
            return
        business = _pick_business(business_data) if business_data is not None else {}
        business_id = business.get("id") or business.get("business_id")
        await _auto_new_client(
            message, parsed, business, business_id, chat_diller_id, token
        )
        return

    # --- Фискальный модуль изменён / Адрес изменён: по фискальному ---
    from handlers.users.find_business import get_business_by_name

    business = {}
    business_id = None
    lookup_chain: tuple = ()

    if holati == "Фискальный модуль изменён":
        if len(fiscal_modules) < 2:
            return
        lookup_chain = (fiscal_modules[-2], fiscal_modules[-1])  # old, new
    elif holati == "Адрес изменён":
        if not fiscal_modules:
            return
        lookup_chain = (fiscal_modules[-1],)
    else:
        return  # неизвестный статус — игнорим

    for fn in lookup_chain:
        if not fn:
            continue
        try:
            response = await get_business_by_name(fn, token)
        except EposAPIError:
            logging.exception("get_business_by_name(%r) failed", fn)
            response = None
        picked = _pick_business(response) if response is not None else {}
        picked_id = picked.get("id") or picked.get("business_id")
        if picked_id:
            business = picked
            business_id = picked_id
            break

    if not business_id:
        await message.reply(
            f"⚠️ Бизнес не найден в Cazad ни по одному из фискальных номеров: "
            + ", ".join(
                f"<code>{html.escape(str(fn))}</code>" for fn in lookup_chain if fn
            )
        )
        return

    # Ownership check.
    business_diller = _flatten_fk(business.get("diller"))
    if business_diller != chat_diller_id:
        await message.reply("⛔ Этот клиент не принадлежит вашему дилеру.")
        return

    if holati == "Фискальный модуль изменён":
        await _auto_fiscal_change(message, parsed, business, business_id, token)
    elif holati == "Адрес изменён":
        await _auto_address_change(message, parsed, business, business_id, token)


async def _auto_new_client(
    message: types.Message,
    parsed: dict,
    business: dict,
    business_id,
    chat_diller_id: int,
    token: str,
) -> None:
    if not business_id:
        zavod = parsed.get("zavod") or "—"
        await message.reply(
            f"⚠️ Бизнес с virtual_number=<code>{html.escape(str(zavod))}</code> "
            f"не найден в базе Cazad. Регистрация невозможна."
        )
        return

    if business.get("auth_key"):
        await message.reply("ℹ️ Этот клиент уже зарегистрирован.")
        return

    fiscal_modules = parsed.get("fiskal_modules") or []
    new_fiscal = fiscal_modules[-1] if fiscal_modules else None
    organization = parsed.get("organization") or "—"
    stir = parsed.get("stir")
    address = parsed.get("address")
    business_type = parsed.get("business_type")

    if not new_fiscal:
        return

    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["name"] = new_fiscal
    payload["diller"] = chat_diller_id
    payload["TIN"] = stir
    payload["pinfl_tin"] = stir
    payload["blocked_date"] = _calc_blocked_date()
    if business_type is not None:
        payload["business_type"] = business_type
    # auth_key сохраняем тот, что уже есть в Cazad (через _flatten_fk выше).

    try:
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        logging.exception("update_business failed in group new-client flow")
        await message.reply(f"⚠️ update_business: {html.escape(str(e))}")
        return

    branches = business.get("branches") or []
    target = next(
        (b for b in branches if isinstance(b, dict) and b.get("id")), None
    )
    try:
        if target:
            branch_id = target["id"]
            branch_payload = {
                key: _flatten_fk(target.get(key))
                for key in BRANCH_UPDATABLE_FIELDS
                if key in target
            }
            branch_payload["name"] = organization
            branch_payload["address"] = address
            branch_payload["contact_person"] = "User"
            branch_payload["contact_phone"] = "+998"
            branch_payload["business"] = business_id
            await update_branch(branch_id, token, **branch_payload)
        else:
            await create_branch(
                token,
                name=organization,
                address=address,
                contact_person="User",
                contact_phone="+998",
                business=business_id,
                city=None,
            )
    except EposAPIError as e:
        logging.exception("branch update/create failed in group new-client flow")
        await message.reply(
            f"⚠️ branch: {html.escape(str(e))} (business уже обновлён)"
        )
        return

    await message.reply("✅ Новый клиент добавлен.")


async def _auto_fiscal_change(
    message: types.Message,
    parsed: dict,
    business: dict,
    business_id,
    token: str,
) -> None:
    """
    Логика по виртуальному номеру (zavod) из PDF:
      1. Бизнес уже подтянут — у нас в `business.name` сейчас стоит то, что
         в базе Cazad.
      2. Сравниваем `name` с фискальными из PDF:
           * name == new_fiscal  -> уже изменено, шлём «уже изменён» и выходим
           * name == old_fiscal  -> заменяем на new_fiscal через update_business
                                    и шлём «обновлено»
           * иначе               -> ни старый, ни новый не совпадают, ничего
                                    не трогаем, репортим конфликт
    """
    fiscal_modules = parsed.get("fiskal_modules") or []
    if len(fiscal_modules) < 2:
        return  # status подразумевает ≥2 строк; защита на всякий случай

    new_fiscal = fiscal_modules[-1]
    old_fiscal = fiscal_modules[-2]
    api_name = business.get("name")

    if api_name == new_fiscal:
        await message.reply(
            f"ℹ️ Фискальный модуль уже изменён в базе.\n"
            f"<b>Текущий id:</b> <code>{html.escape(str(api_name))}</code>"
        )
        return

    if api_name != old_fiscal:
        await message.reply(
            f"⚠️ Текущий фискальный модуль в базе не совпадает с PDF.\n"
            f"<b>В базе:</b> <code>{html.escape(str(api_name))}</code>\n"
            f"<b>В PDF (старый):</b> <code>{html.escape(str(old_fiscal))}</code>\n"
            f"<b>В PDF (новый):</b> <code>{html.escape(str(new_fiscal))}</code>"
        )
        return

    # api_name == old_fiscal — апдейтим в Cazad. Все остальные поля
    # сохраняются через _flatten_fk, перезаписываем только name.
    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["name"] = new_fiscal

    try:
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        logging.exception("update_business failed in group fiscal flow")
        await message.reply(f"⚠️ update_business: {html.escape(str(e))}")
        return

    await message.reply(
        f"✅ Фискальный модуль обновлён.\n"
        f"<b>Было:</b> <code>{html.escape(str(old_fiscal))}</code>\n"
        f"<b>Стало:</b> <code>{html.escape(str(new_fiscal))}</code>"
    )

    business_diller_id = _flatten_fk(business.get("diller"))
    user = message.from_user
    diller_name = await get_user_diller_name(user.id) or "—"
    summary = (
        f"🔁 <b>Заменён фискальный модуль</b> (auto, группа)\n"
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"<b>Старый:</b> <code>{html.escape(str(old_fiscal))}</code>\n"
        f"<b>Новый:</b> <code>{html.escape(str(new_fiscal))}</code>"
    )
    try:
        await notify_log_groups(business_diller_id, summary)
    except Exception as exc:
        logging.exception(f"group fiscal notify_log_groups failed: {exc}")


async def _auto_address_change(
    message: types.Message,
    parsed: dict,
    business: dict,
    business_id,
    token: str,
) -> None:
    """Для статуса 'Адрес изменён' — обновляем branch.address.
    Имя фирмы (branch.name) НЕ трогаем в групповом flow."""
    new_address = (parsed.get("address") or "").strip()
    if not new_address or new_address == "—":
        return

    branches = business.get("branches") or []
    target = next(
        (b for b in branches if isinstance(b, dict) and b.get("id")), None
    )
    if not target:
        await message.reply("⚠️ У клиента нет филиалов для обновления.")
        return

    api_address = (target.get("address") or "").strip()
    if api_address.lower() == new_address.lower():
        await message.reply("ℹ️ Адрес уже актуален.")
        return

    branch_id = target["id"]
    payload = {
        key: _flatten_fk(target.get(key))
        for key in BRANCH_UPDATABLE_FIELDS
        if key in target
    }
    payload["address"] = new_address
    payload["business"] = business_id

    try:
        await update_branch(branch_id, token, **payload)
    except EposAPIError as e:
        logging.exception("update_branch failed in group address flow")
        await message.reply(f"⚠️ update_branch: {html.escape(str(e))}")
        return

    await message.reply(
        f"✅ Адрес обновлён.\n"
        f"<b>Было:</b> <code>{html.escape(api_address or '—')}</code>\n"
        f"<b>Стало:</b> <code>{html.escape(new_address)}</code>"
    )

    business_diller_id = _flatten_fk(business.get("diller"))
    user = message.from_user
    diller_name = await get_user_diller_name(user.id) or "—"
    summary = (
        f"📍 <b>Адрес обновлён</b> (auto, группа)\n"
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"<b>Было:</b> <code>{html.escape(api_address or '—')}</code>\n"
        f"<b>Стало:</b> <code>{html.escape(new_address)}</code>"
    )
    try:
        await notify_log_groups(business_diller_id, summary)
    except Exception as exc:
        logging.exception(f"group address notify_log_groups failed: {exc}")


async def _send_doc_with_text(chat_id, document, text: str) -> None:
    if len(text) <= CAPTION_LIMIT:
        await bot.send_document(chat_id=chat_id, document=document, caption=text)
    else:
        await bot.send_document(chat_id=chat_id, document=document)
        await bot.send_message(chat_id, text)


# ---------------------------------------------------------------
# Prodleniya (renewal) via plain-text messages in the same
# registration groups that accept PDFs. Format: each line has one
# or more VG-fiscals + dates (dd.mm.yyyy). Last date + 1 day
# becomes the new blocked_date. Anyone in the group can post;
# the chat's linked diller is used to validate ownership of each
# matched business.
# ---------------------------------------------------------------


@dp.message_handler(
    lambda m: bool(m.text and FISCAL_RE.search(m.text)),
    chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP],
    content_types=types.ContentType.TEXT,
)
async def handle_group_prodleniya(message: types.Message):
    logging.info(
        "prodleniya: text in chat=%s user=%s len=%s preview=%r",
        message.chat.id, message.from_user.id,
        len(message.text or ""), (message.text or "")[:100],
    )
    chat_row = await db.get_chat(message.chat.id)
    if not chat_row or chat_row["status"] != "approved":
        return
    if chat_row.get("group_type") != "registration":
        return
    if not chat_row.get("prodleniya_enabled"):
        return

    chat_diller_id = chat_row.get("diller_id")
    if chat_diller_id is None:
        return

    entries = parse_prodleniya_text(message.text or "")
    if not entries:
        return

    user = message.from_user
    full_name = html.escape(user.full_name)
    diller_name_sender = await get_user_diller_name(user.id) or "—"

    enqueue_ctx = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "user_id": user.id,
        "diller_id": chat_diller_id,
    }

    # Buckets for the reply summary.
    updated_both: List[Tuple[str, str]] = []
    updated_cazad: List[Tuple[str, str]] = []
    updated_mgmt: List[Tuple[str, str]] = []
    no_op: List[Tuple[str, str, str]] = []   # (fiscal, current_iso, target)
    not_found: List[str] = []
    queued: List[Tuple[str, str]] = []
    errors: List[Tuple[str, str]] = []

    for entry in entries:
        target_iso = entry["new_blocked_date"]

        # Prodleniya spec: try each candidate fiscal in the entry. The first
        # one that is found in at least one backend is the winner — we don't
        # touch the rest (they're likely old/new copies of the same cashdesk).
        winning: Optional[ProdOutcome] = None
        outcomes: List[ProdOutcome] = []
        for fn in entry["fiscals"]:
            outcome = await process_fiscal(
                fn,
                target_iso,
                enqueue_context=enqueue_ctx,
            )
            outcomes.append(outcome)
            found_somewhere = (
                outcome.cazad_skipped_reason not in (None, "not_found", "error")
                or outcome.cazad_updated
                or outcome.mgmt_skipped_reason not in (None, "not_found", "error")
                or outcome.mgmt_updated
            )
            if found_somewhere:
                winning = outcome
                break

        if winning is None:
            # Не найден нигде. Если попутно ловили API-ошибку — сообщим.
            first_err = next(
                (
                    o.cazad_error or o.mgmt_error
                    for o in outcomes
                    if o.cazad_error or o.mgmt_error
                ),
                None,
            )
            if first_err:
                errors.append((", ".join(entry["fiscals"]), first_err))
            else:
                not_found.append(", ".join(entry["fiscals"]))
            continue

        o = winning
        fn = o.fiscal

        # Update result classification.
        both_up = o.cazad_updated and o.mgmt_updated
        only_cazad = o.cazad_updated and not o.mgmt_updated
        only_mgmt = o.mgmt_updated and not o.cazad_updated

        if both_up:
            updated_both.append((fn, target_iso))
        elif only_cazad:
            updated_cazad.append((fn, target_iso))
        elif only_mgmt:
            updated_mgmt.append((fn, target_iso))

        # No-op if both backends already at/beyond target (nothing changed).
        if (
            not (o.cazad_updated or o.mgmt_updated)
            and (o.cazad_skipped_reason == "no_op"
                 or o.mgmt_skipped_reason == "no_op")
        ):
            current = o.cazad_current_iso or o.mgmt_current_iso or "—"
            no_op.append((fn, str(current)[:10], target_iso))

        if o.mgmt_skipped_reason == "queued":
            queued.append((fn, target_iso))

        if o.cazad_error:
            errors.append((fn, f"cazad: {o.cazad_error}"))
        if o.mgmt_error and o.mgmt_skipped_reason != "queued":
            errors.append((fn, f"mgmt: {o.mgmt_error}"))

        # Log-groups notify — только когда block date реально изменилась
        # хотя бы в одном бэкенде (cazad ИЛИ management). no_op не шлём.
        if o.cazad_updated or o.mgmt_updated:
            tin = o.business_tin or "—"
            biz_name = o.business_name or "—"
            summary = (
                "🔒 <b>Обновлена дата блокировки</b>\n"
                f"<b>Diller:</b> {html.escape(str(diller_name_sender))}\n"
                f"<b>От:</b> {full_name} "
                f"(id: <code>{user.id}</code>)\n\n"
                f"<b>Фискальный номер:</b> <code>{html.escape(fn)}</code>\n"
                f"<b>ИНН:</b> <code>{html.escape(str(tin))}</code>\n"
                f"<b>Название бизнеса:</b> {html.escape(str(biz_name))}\n"
                f"<b>Дата блокировки:</b> <code>{html.escape(target_iso)}</code>"
            )
            try:
                await notify_log_groups(chat_diller_id, summary)
            except Exception as exc:
                logging.exception(
                    f"group prodleniya notify_log_groups failed for {fn}: {exc}"
                )

    # Build reply.
    def _fmt_list(rows, prefix_kv=False):
        out = []
        for row in rows[:10]:
            if prefix_kv:
                fn, cur, tgt = row
                out.append(
                    f"  • <code>{html.escape(fn)}</code>: "
                    f"<code>{cur}</code> ≥ <code>{tgt}</code>"
                )
            else:
                fn, iso = row
                out.append(f"  • <code>{html.escape(fn)}</code> → <code>{iso}</code>")
        if len(rows) > 10:
            out.append(f"  …ещё {len(rows) - 10}")
        return out

    lines: List[str] = []
    if updated_both:
        lines.append(f"✅ Продлено (cazad + management): <b>{len(updated_both)}</b>")
        lines.extend(_fmt_list(updated_both))
    if updated_cazad:
        lines.append(f"\n✅ Продлено только в cazad: <b>{len(updated_cazad)}</b>")
        lines.extend(_fmt_list(updated_cazad))
    if updated_mgmt:
        lines.append(f"\n✅ Продлено только в management: <b>{len(updated_mgmt)}</b>")
        lines.extend(_fmt_list(updated_mgmt))
    if no_op:
        lines.append(f"\nℹ️ Уже актуально (не откатываем): <b>{len(no_op)}</b>")
        lines.extend(_fmt_list(no_op, prefix_kv=True))
    if queued:
        lines.append(
            f"\n⏳ Отложено (management сейчас не отвечает): <b>{len(queued)}</b>"
        )
        lines.extend(_fmt_list(queued))
        lines.append(
            "  Повторим сами каждые 2 мин. По результату ответим в этом чате."
        )
    if not_found:
        lines.append(f"\n⚠️ Не найдено нигде: <b>{len(not_found)}</b>")
        for s in not_found[:10]:
            lines.append(f"  • <code>{html.escape(s)}</code>")
        if len(not_found) > 10:
            lines.append(f"  …ещё {len(not_found) - 10}")
    if errors:
        lines.append(f"\n❌ Ошибки API: <b>{len(errors)}</b>")
        for fn, err in errors[:5]:
            lines.append(
                f"  • <code>{html.escape(fn)}</code>: {html.escape(err[:120])}"
            )

    if not_found or errors or queued:
        lines.append(f"\n👉 @{TECH_SUPPORT_USERNAME}, проверьте пожалуйста.")

    if lines:
        await message.reply("\n".join(lines))
