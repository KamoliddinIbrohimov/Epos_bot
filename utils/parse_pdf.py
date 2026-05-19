import re
from typing import Dict, List, Optional, Tuple

import pdfplumber


class PdfParseError(Exception):
    pass


def _normalize(s: Optional[str]) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s).strip()


DEFAULT_BUSINESS_TYPE = 4


def _detect_business_type(legal_form_text: Optional[str], stir: Optional[str]) -> int:
    """Map PDF legal form text (or 14-digit STIR) to business_type id.

    MAS'ULIYATI CHEKLANGAN JAMIYAT / MCHJ -> 4
    OILAVIY KORXONA              / OK   -> 6
    XUSUSIY KORXONA              / XK   -> 2
    STIR has 14 digits (YATT)           -> 1
    Anything else / not detectable      -> 4 (default)
    """
    if stir and len(stir) == 14:
        return 1
    if not legal_form_text:
        return DEFAULT_BUSINESS_TYPE

    # Normalize: uppercase, collapse whitespace, unify apostrophes.
    text = legal_form_text.upper()
    text = re.sub(r"['‘’ʻʼ']", "'", text)
    text = re.sub(r"\s+", " ", text).strip()

    if "MAS'ULIYATI CHEKLANGAN JAMIYAT" in text or re.search(r"\bMCHJ\b", text):
        return 4
    if "OILAVIY KORXONA" in text or re.search(r"\bOK\b", text):
        return 6
    if "XUSUSIY KORXONA" in text or re.search(r"\bXK\b", text):
        return 2
    return DEFAULT_BUSINESS_TYPE


def _parse_header(
    text: str,
) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Extract organization name, STIR, and business_type from PDF header text.

    Cases:
      * Legal entity (STIR = 9 digits): name comes in quotes, followed by
        legal form (MChJ / OAJ / ...). We extract ONLY the quoted name and
        derive business_type from the legal form text after the closing quote.
      * YATT (STIR = 14 digits): name has no quotes. We take the full name
        between the `ma'lumot` marker and `(STIR:`; business_type = 1.
    """
    m_stir = re.search(r"STIR:\s*(\d+)", text)
    if not m_stir:
        return None, None, None, None
    stir = m_stir.group(1)

    # YATT case (14-digit STIR / PINFL): take the full name as-is, no quotes.
    if len(stir) == 14:
        m = re.search(
            r"ma['’]lumot\s+(.+?)\s*\(\s*STIR:\s*" + re.escape(stir),
            text,
            re.DOTALL,
        )
        name = re.sub(r"\s+", " ", m.group(1).strip()) if m else None
        return name, stir, 1, "YATT"

    # Legal entity: quoted name + legal form text + (STIR: ...). Принимаем любые
    # парные кавычки (ASCII " ", гильметы « », немецкие „ ", curly " "), и
    # терпим переносы строк между legal form и `(STIR:` — у некоторых PDF между
    # ними реально стоит \n.
    quote = r'["«»“”„]'
    m = re.search(
        quote + r"(.+?)" + quote + r"\s+(.+?)\s*\(\s*STIR:\s*" + re.escape(stir),
        text,
        re.DOTALL,
    )
    if m:
        name = m.group(1).strip()
        legal_form = re.sub(r"\s+", " ", m.group(2).strip())
        return name, stir, _detect_business_type(legal_form, stir), legal_form

    # Fallback: take whatever comes before "(STIR:" if quotes weren't found.
    m = re.search(
        r"ma['’]lumot\s+(.+?)\s*\(\s*STIR:\s*" + re.escape(stir),
        text,
        re.DOTALL,
    )
    if m:
        name = re.sub(r"\s+", " ", m.group(1).strip())
        return name, stir, _detect_business_type(name, stir), None

    return None, stir, None, None


def _compute_statuses(rows: List[Dict]) -> List[Dict]:
    """Apply business rules to compute per-row status."""
    out = []
    for i, row in enumerate(rows):
        new_row = dict(row)
        is_epos = row["kassa_name"].upper() == "E-POS"
        if not is_epos:
            new_row["status"] = "—"
        elif i == 0:
            new_row["status"] = "Новый клиент"
        else:
            prev = rows[i - 1]
            fisk_changed = row["fiskal_modul"] != prev["fiskal_modul"]
            addr_changed = row["address"] != prev["address"]
            if fisk_changed:
                new_row["status"] = "Фискальный модуль изменён"
            elif addr_changed:
                new_row["status"] = "Адрес изменён"
            else:
                new_row["status"] = "Новый клиент"
        out.append(new_row)
    return out


def parse_business_pdf(path: str) -> dict:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        tables = page.extract_tables()

    if not text.strip():
        raise PdfParseError("PDF dan matnni o'qib bo'lmadi")
    if not tables:
        raise PdfParseError("PDF da jadval topilmadi")

    organization, stir, business_type, activity_type = _parse_header(text)

    rows = []
    for raw_row in tables[0][1:]:  # skip header row
        if not raw_row or len(raw_row) < 7:
            continue
        rows.append({
            "reyestr": _normalize(raw_row[0]),
            "kassa_name": _normalize(raw_row[1]),
            "zavod": _normalize(raw_row[2]),
            "fiskal_modul": _normalize(raw_row[3]),
            "address": _normalize(raw_row[4]),
            "status_raw": _normalize(raw_row[5]),
            "date": _normalize(raw_row[6]),
        })

    if not rows:
        raise PdfParseError("Jadvalda ma'lumotlar topilmadi")

    rows = _compute_statuses(rows)
    epos_rows = [r for r in rows if r["kassa_name"].upper() == "E-POS"]
    primary = epos_rows[-1] if epos_rows else rows[-1]

    return {
        "organization": organization or "—",
        "stir": stir or "—",
        "business_type": business_type if business_type is not None else DEFAULT_BUSINESS_TYPE,
        "activity_type": activity_type or "—",
        "zavod": primary["zavod"] or "—",
        "fiskal_modules": [r["fiskal_modul"] for r in rows],
        "address": primary["address"].capitalize() if primary["address"] else "—",
        "holati": epos_rows[-1]["status"] if epos_rows else "Topilmadi",
    }


def format_analysis(parsed: dict) -> str:
    fiskal_lines = "\n====\n".join(parsed["fiskal_modules"])
    return (
        "📋 Tahlil natijasi\n\n"
        f"🏢 Tashkilot - {parsed['organization']}\n"
        f"🏷 Faoliyat turi - {parsed['activity_type']}\n\n"
        f"🆔 STIR - {parsed['stir']}\n"
        f"⚙️ Zavod № - {parsed['zavod']}\n\n"
        f"📟 Fiskal modul raqami - {fiskal_lines}\n\n"
        f"📍 Manzil - {parsed['address']}\n\n"
        f"📋 Holati - {parsed['holati']}"
    )
