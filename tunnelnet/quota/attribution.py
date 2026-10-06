"""Attribution and aggregation over the immutable, point in time journal."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .model import UsageEntry
from .overage import overage_for
from .shifts import Shift, containing


@dataclass(frozen=True)
class ShiftAttribution:
    """One cabin's consumption in one shift, judged by that shift's baseline."""

    cabin_id: str
    shift: Shift
    watts: float
    baseline_watts: float
    allocation_id: str
    overage: float

    def as_dict(self) -> dict[str, object]:
        return {
            "cabin_id": self.cabin_id,
            "shift": self.shift.as_dict(),
            "watts": self.watts,
            "baseline_watts": self.baseline_watts,
            "allocation_id": self.allocation_id,
            "overage": self.overage,
        }


def attribute(entries: list[UsageEntry]) -> list[ShiftAttribution]:
    """Group events per cabin and shift and reconcile with the shift baseline.

    Every event already carries the baseline resolved for its own moment. An
    adjustment only changes the baseline at a shift start, so all events of one
    (cabin, shift) share one baseline; the first event's snapshot is the
    reconciliation basis for the whole shift.
    """

    grouped: dict[tuple[str, str], list[UsageEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.cabin_id, entry.shift_start)].append(entry)

    rows: list[ShiftAttribution] = []
    for (cabin_id, shift_start), events in grouped.items():
        watts = sum(event.watts for event in events)
        baseline = events[0].baseline_watts
        allocation_id = events[0].allocation_id
        rows.append(
            ShiftAttribution(
                cabin_id=cabin_id,
                shift=containing(shift_start),
                watts=watts,
                baseline_watts=baseline,
                allocation_id=allocation_id,
                overage=overage_for(watts, baseline),
            )
        )
    return sorted(rows, key=lambda row: (row.shift.start, row.cabin_id))


def totals(rows: list[ShiftAttribution]) -> tuple[float, float]:
    return (
        sum(row.watts for row in rows),
        sum(row.overage for row in rows),
    )
