import random
from datetime import date, timedelta
from typing import Set


def adjust_block_date(target: date, holidays: Set[date]) -> date:
    """Adjust a proposed block date to a valid working day.

    Rules (re-checked after each shift):
      1. Day-1 randomisation: if the date is the 1st of the month,
         pick a random day between 1 and 10 (inclusive) of that month.
      2. Weekend skip: Saturday → +2 days (Monday), Sunday → +1 day.
      3. Holiday skip: if the resulting date is in *holidays*, advance by 1 day
         and re-check rules 2–3 until a clean working day is found.
    """
    if target.day == 1:
        target = target.replace(day=random.randint(1, 10))

    while True:
        wd = target.weekday()  # 0=Mon … 6=Sun
        if wd == 5:            # Saturday → Monday
            target += timedelta(days=2)
        elif wd == 6:          # Sunday → Monday
            target += timedelta(days=1)
        elif target in holidays:
            target += timedelta(days=1)
        else:
            break

    return target
