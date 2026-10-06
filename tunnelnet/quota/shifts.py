"""Fixed eight hour shift timetable.

The tunnel runs three shifts per day. Shifts never move: every shift starts
on a day boundary at 00:00, 08:00 or 16:00 UTC. Baseline adjustments only take
effect at shift starts, so the baseline in force during one shift is constant
and the historical accounting for that shift never changes later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SHIFT_HOURS = 8
SHIFT_NAMES = ("A", "B", "C")
EPOCH = "1970-01-01T00:00:00Z"


class ShiftError(ValueError):
    """Raised when a timestamp cannot be placed on the shift timetable."""


@dataclass(frozen=True)
class Shift:
    """One eight hour work shift."""

    start: str
    name: str
    index: int

    @property
    def end(self) -> str:
        return format_time(parse(self.start) + timedelta(hours=SHIFT_HOURS))

    @property
    def day(self) -> str:
        return self.start[:10]

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "name": self.name, "index": self.index}


def parse(value: str) -> datetime:
    """Parse the ``YYYY-MM-DDTHH:MM:SSZ`` stamps used across the console."""

    if not value:
        raise ShiftError("timestamp is empty")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ShiftError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor(moment: datetime) -> datetime:
    seconds = SHIFT_HOURS * 3600
    epoch_seconds = int(moment.timestamp())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % seconds, tz=timezone.utc)


def containing(at: str) -> Shift:
    """Shift in progress at ``at`` (a boundary stamp belongs to the new shift)."""

    moment = parse(at)
    start = _floor(moment)
    index = (start.hour // SHIFT_HOURS) % len(SHIFT_NAMES)
    return Shift(start=format_time(start), name=SHIFT_NAMES[index], index=index)


def next_start(at: str) -> str:
    """Start stamp of the first shift strictly after the submission moment.

    A submission made exactly on a boundary still takes effect on the
    following shift; at any other moment it takes effect on the upcoming
    boundary.
    """

    moment = parse(at)
    boundary = _floor(moment) + timedelta(hours=SHIFT_HOURS)
    return format_time(boundary)


def shift_for_effective(effective_at: str) -> Shift:
    """Validate that a requested effective stamp is a shift boundary."""

    moment = parse(effective_at)
    if moment != _floor(moment):
        raise ShiftError(f"effective time {effective_at!r} is not a shift start")
    return containing(effective_at)
