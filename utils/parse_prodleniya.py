"""Parse plain-text 'prodleniya' (renewal) messages.

Format is loose — each line is one business entry. From a line we extract:
  * one or more fiscal module numbers  (tokens matching `VG\\d+`, case-insensitive)
  * one or more calendar dates          (`dd.mm.yyyy`)

We treat the **last** date on the line as the new expire date and add
+1 day to get the `blocked_date` to send to Cazad (business is blocked
starting the day after the paid period ends).

Everything else on the line (zavod seryas, firm name, INN, etc.) is ignored.

A line is considered valid only if it contains at least one fiscal and
at least one date. Empty/comment lines are skipped silently.

Example line::

    VG300750016617   VG300750016619  936661   983690  01.07.2022  15.08.2026  ДИЁРА ...  303058794
    ^ fiscals -------^                                 ^ contract  ^ new-exp

Parsed as::

    {
        "fiscals": ["VG300750016617", "VG300750016619"],
        "new_blocked_date": "2026-08-16",   # 15.08.2026 + 1 day, ISO
        "raw": "<original line>",
    }
"""

import re
from datetime import date, datetime, timedelta
from typing import List, TypedDict


FISCAL_RE = re.compile(r"\b[A-Z]{2}\d{10,}\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


class ProdleniyaEntry(TypedDict):
    fiscals: List[str]
    new_blocked_date: str  # ISO 'YYYY-MM-DD'
    raw: str


class ProdleniyaParseError(Exception):
    pass


def _parse_date(day: str, month: str, year: str) -> date:
    return datetime.strptime(f"{day}.{month}.{year}", "%d.%m.%Y").date()


def parse_prodleniya_text(text: str) -> List[ProdleniyaEntry]:
    """Parse a multi-line prodleniya text into a list of entries.

    Invalid lines (no fiscal, no date, unparseable date) are skipped.
    Fiscal tokens are upper-cased for consistency with Cazad's `name`.
    """
    entries: List[ProdleniyaEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        fiscals = [m.group(0).upper() for m in FISCAL_RE.finditer(line)]
        if not fiscals:
            continue

        date_matches = list(DATE_RE.finditer(line))
        if not date_matches:
            continue

        last = date_matches[-1]
        try:
            expire = _parse_date(last.group(1), last.group(2), last.group(3))
        except ValueError:
            continue

        blocked = expire + timedelta(days=1)
        entries.append(
            {
                "fiscals": fiscals,
                "new_blocked_date": blocked.isoformat(),
                "raw": line,
            }
        )

    return entries
