"""NYSE trading calendar: full holidays, early closes (13:00 ET) and known special closures.

Rules (NYSE Rule 7.2 and published holiday schedules):
* New Year's Day (Sunday -> Monday; Saturday -> not observed), Martin Luther King Jr. Day (3rd Monday
  of January), Washington's Birthday (3rd Monday of February), Good Friday, Memorial Day (last
  Monday of May), Juneteenth (from 2022), Independence Day, Labor Day (1st Monday of September),
  Thanksgiving (4th Thursday of November), Christmas. Saturday holidays close the preceding
  Friday, Sunday holidays the following Monday.
* Early close at 13:00 ET on July 3, the day after Thanksgiving and December 24, when those are
  trading days.
* Unscheduled closures (e.g. national days of mourning) are listed explicitly and must be added
  when announced.

Settlement (T+1) follows the stock-market calendar here; bank holidays on which markets are open
(Columbus Day, Veterans Day) are not modelled, so the broker remains the source of truth for
settled cash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2025, 1, 9),  # national day of mourning, President Jimmy Carter
    }
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - name from the published algorithm
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    out: set[date] = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        out.add(new_year + timedelta(days=1))
    elif new_year.weekday() < 5:
        out.add(new_year)
    out.add(_nth_weekday(year, 1, 0, 3))  # MLK
    out.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    out.add(easter(year) - timedelta(days=2))  # Good Friday
    out.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        jt = _observed(date(year, 6, 19))
        if jt is not None:
            out.add(jt)
    ind = _observed(date(year, 7, 4))
    if ind is not None:
        out.add(ind)
    out.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    xmas = _observed(date(year, 12, 25))
    if xmas is not None:
        out.add(xmas)
    out |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(out)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset[date]:
    candidates = {date(year, 7, 3), _nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24)}
    return frozenset(d for d in candidates if is_trading_day(d))


def close_time(d: date) -> time:
    return EARLY_CLOSE if d in early_closes(d.year) else CLOSE


def session_open(d: date) -> datetime:
    return datetime.combine(d, OPEN, tzinfo=NEW_YORK).astimezone(UTC)


def session_close(d: date) -> datetime:
    return datetime.combine(d, close_time(d), tzinfo=NEW_YORK).astimezone(UTC)


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def is_regular_session(ts: datetime) -> bool:
    local = ts.astimezone(NEW_YORK)
    d = local.date()
    if not is_trading_day(d):
        return False
    return OPEN <= local.time() < close_time(d)


def first_session_close_after(ts: datetime) -> datetime:
    """Close of the first session in which an order sent at ``ts`` can trade (DAY orders sent while
    the market is closed live until the end of the next session)."""
    d = ts.astimezone(NEW_YORK).date()
    if is_trading_day(d) and ts < session_close(d):
        return session_close(d)
    return session_close(next_trading_day(d))
