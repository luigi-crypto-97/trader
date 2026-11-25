from __future__ import annotations

from datetime import UTC, date, datetime

from trader.broker.calendar import (
    early_closes,
    first_session_close_after,
    holidays,
    is_regular_session,
    is_trading_day,
    next_trading_day,
)


def test_published_nyse_holidays_2025_2026() -> None:
    assert holidays(2025) == {
        date(2025, 1, 1),
        date(2025, 1, 9),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
    }
    assert holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
    assert early_closes(2025) == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
    assert early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}  # July 3 is a holiday in 2026


def test_observed_rules_2027() -> None:
    h = holidays(2027)
    assert date(2027, 6, 18) in h and date(2027, 7, 5) in h and date(2027, 12, 24) in h
    assert date(2027, 12, 31) not in h  # New Year 2028 on Saturday is not observed


def test_sessions_and_closes() -> None:
    et = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)  # noqa: E731 (UTC = ET+5 in Nov)
    friday = date(2026, 11, 27)
    assert is_regular_session(et(friday, 17, 0))  # 12:00 ET on the half day
    assert not is_regular_session(et(friday, 18, 30))  # 13:30 ET: already closed
    assert not is_regular_session(et(date(2026, 11, 26), 16, 0))  # Thanksgiving
    assert not is_trading_day(date(2026, 11, 28)) and next_trading_day(friday) == date(2026, 11, 30)
    assert first_session_close_after(et(date(2026, 11, 26), 16, 0)) == et(friday, 18, 0)
    assert first_session_close_after(et(friday, 15, 0)) == et(friday, 18, 0)
