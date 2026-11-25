"""Market data primitives shared by the price loader, the backtester and research code.

Bars are *unadjusted* (as traded on that day). Splits are applied explicitly on their execution
date; see :func:`split_factor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


PriceData = dict[str, dict[date, Bar]]


@dataclass(frozen=True)
class Split:
    ticker: str
    execution_date: date
    split_from: Decimal
    split_to: Decimal

    @property
    def ratio(self) -> Decimal:
        """New shares per old share: quantities multiply by it, prices divide by it."""
        return self.split_to / self.split_from


SplitMap = dict[str, list[Split]]


def split_factor(splits: list[Split] | None, after: date, upto: date) -> Decimal:
    """Product of split ratios executed in (after, upto].

    A share held from the open of ``after`` to the close of ``upto`` becomes this many shares.
    Splits executed *on* ``after`` are excluded: that day's open is already post-split.
    """
    factor = Decimal("1")
    for s in splits or ():
        if after < s.execution_date <= upto:
            factor *= s.ratio
    return factor
