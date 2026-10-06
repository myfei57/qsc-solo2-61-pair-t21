"""Single caliber projections shared by allocation views and console export.

Baseline allocation, usage attribution and the CSV export all read the same
winning allocations and the same frozen usage events from the ledger, so a row
on the operator export matches the shift's overage exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .overage import overage_for
from .service import QuotaService


@dataclass(frozen=True)
class AttributionRow:
    """One cabin's baseline, usage and overage for one shift."""

    cabin_id: str
    shift_label: str
    baseline_watts: float
    usage_watts: float
    overage_watts: float
    batch_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "cabin_id": self.cabin_id,
            "shift_label": self.shift_label,
            "baseline_watts": self.baseline_watts,
            "usage_watts": self.usage_watts,
            "overage_watts": self.overage_watts,
            "batch_id": self.batch_id,
        }


def attribution_rows(service: QuotaService, shift_label: str = "") -> list[AttributionRow]:
    """Freeze one row per cabin that has an allocation or usage in the shift."""

    target = shift_label or service.current_shift_label()
    usage = service.shift_usage(target)
    rows: list[AttributionRow] = []
    cabin_ids = set(usage)
    winning = {allocation.cabin_id: allocation for allocation in service.allocation_rows(target)}
    cabin_ids.update(winning)
    for cabin_id in sorted(cabin_ids):
        allocation = winning.get(cabin_id)
        baseline = allocation.watts if allocation is not None else service.baseline_watts(cabin_id, target)
        batch_id = allocation.batch_id if allocation is not None else "default"
        used = usage.get(cabin_id, 0.0)
        rows.append(
            AttributionRow(
                cabin_id=cabin_id,
                shift_label=target,
                baseline_watts=baseline,
                usage_watts=used,
                overage_watts=overage_for(used, baseline),
                batch_id=batch_id,
            )
        )
    return rows
