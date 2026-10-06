"""Shift calendar.

The chosen accounting caliber is ``NEXT_SHIFT``: a baseline adjustment takes
effect at the start of the next shift, never inside the running shift. Every
shift is therefore charged against exactly one frozen baseline and historical
attribution can never change when the baseline is adjusted later.

A shift label is the compact ``YYYY-MM-DD#n`` string (for example
``2026-10-05#1``). With the standard roster of at most nine shifts a day it
sorts lexicographically in chronological order (ISO date, then the single
digit index), so the ledger can treat "the current allocation" as the
greatest label at or below a shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LABEL_SEP = "#"


@dataclass(frozen=True)
class Shift:
    """One work shift: the calendar day it belongs to and its index that day."""

    day: str
    index: int

    @property
    def label(self) -> str:
        return f"{self.day}{LABEL_SEP}{self.index}"

    def as_dict(self) -> dict[str, object]:
        return {"day": self.day, "index": self.index, "label": self.label}


class ShiftCalendar:
    """Splits UTC time into ordered, equal length shifts.

    ``start_hours`` are the hours of day at which a new shift begins, in
    ascending order. The default two shift roster turns over at 08:00 and
    20:00 UTC.
    """

    def __init__(self, start_hours: tuple[int, ...] = (8, 20)) -> None:
        if not start_hours or any(not 0 <= hour < 24 for hour in start_hours):
            raise ValueError("start_hours must be hour values inside [0, 24)")
        if list(start_hours) != sorted(start_hours) or len(set(start_hours)) != len(start_hours):
            raise ValueError("start_hours must be strictly ascending")
        if len(start_hours) > 9:
            raise ValueError("at most nine shifts per day keep labels lexicographically ordered")
        self._hours = start_hours

    @property
    def shifts_per_day(self) -> int:
        return len(self._hours)

    def start_hours(self) -> tuple[int, ...]:
        return self._hours

    def shift_at(self, at: datetime) -> Shift:
        """The shift running at ``at``."""

        moment = _as_utc(at)
        if (moment.hour, moment.minute) < (self._hours[0], 0):
            # Before the first turnover of the day: still on the last shift
            # of the previous calendar day.
            return Shift(day=(moment - timedelta(days=1)).date().isoformat(), index=self.shifts_per_day - 1)
        index = 0
        for position, hour in enumerate(self._hours):
            if (moment.hour, moment.minute) >= (hour, 0):
                index = position
        return Shift(day=moment.date().isoformat(), index=index)

    def next_shift(self, at: datetime) -> Shift:
        """The first shift that has not started at ``at``.

        When ``at`` is exactly on a boundary it belongs to the shift that is
        just starting, so the next shift is the following one.
        """

        moment = _as_utc(at)
        current = self.shift_at(moment)
        if current.index + 1 < self.shifts_per_day:
            return Shift(day=current.day, index=current.index + 1)
        return Shift(day=(moment + timedelta(days=1)).date().isoformat(), index=0)

    def shift_start(self, shift: Shift) -> datetime:
        """UTC instant at which ``shift`` begins."""

        day = datetime.fromisoformat(shift.day).replace(tzinfo=timezone.utc)
        return day.replace(hour=self._hours[shift.index])

    def has_started(self, shift: Shift, at: datetime) -> bool:
        return _as_utc(at) >= self.shift_start(shift)

    def parse_label(self, label: str) -> Shift:
        day, separator, raw_index = label.partition(LABEL_SEP)
        if not separator:
            raise ValueError(f"invalid shift label {label!r}")
        datetime.fromisoformat(day)
        index = int(raw_index)
        if not 0 <= index < self.shifts_per_day:
            raise ValueError(f"shift index {index} outside roster {self._hours}")
        return Shift(day=day, index=index)


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def _hours_minutes(moment: datetime) -> tuple[int, int]:
    return moment.hour, moment.minute


def parse_instant(value: str) -> datetime:
    """Parse the ``...Z`` ISO timestamps used across the console into UTC."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _as_utc(parsed)
