"""Read only energy report."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaReport:
    """Allocated, consumed and over budget totals for one shift."""

    allocated: float
    usage_total: float
    overage_total: float
    peak_cabin: str
    cabins: int
    shift_label: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "allocated": self.allocated,
            "usage_total": self.usage_total,
            "overage_total": self.overage_total,
            "peak_cabin": self.peak_cabin,
            "cabins": self.cabins,
            "shift_label": self.shift_label,
        }
