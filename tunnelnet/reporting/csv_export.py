"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tunnelnet.audit import AuditRegistry
from tunnelnet.quota import QuotaService, attribution_rows
from tunnelnet.store import Store

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from tunnelnet.console.telemetry import Telemetry

QUOTE_TRIGGERS = (",", '"', "\n")


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(trigger in text for trigger in QUOTE_TRIGGERS):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a header row and body rows as comma separated text."""

    lines = [",".join(_escape(column) for column in columns)]
    for row in rows:
        lines.append(",".join(_escape(value) for value in row))
    return "\n".join(lines) + "\n"


def audit_export(registry: AuditRegistry) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the linkage audit trail."""

    columns = ["id", "event", "at"]
    rows = [[record.id, record.event, record.at] for record in registry.list()]
    return columns, rows


def store_export(store: Store) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the record counts of every store kind."""

    stats = store.stats()
    return ["kind", "records"], [[kind, stats[kind]] for kind in sorted(stats)]


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]


def quota_attribution_export(
    quota: QuotaService, shift_label: str = ""
) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for one shift's baseline, usage and overage."""

    columns = ["shift_label", "cabin_id", "baseline_watts", "usage_watts", "overage_watts", "baseline_batch"]
    rows = [
        [
            row.shift_label,
            row.cabin_id,
            row.baseline_watts,
            row.usage_watts,
            row.overage_watts,
            row.batch_id,
        ]
        for row in attribution_rows(quota, shift_label)
    ]
    return columns, rows


def quota_allocation_export(
    quota: QuotaService, shift_label: str = ""
) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the winning baseline of every cabin in a shift."""

    columns = ["shift_label", "cabin_id", "watts", "batch_id", "seq"]
    rows = [
        [allocation.shift_label, allocation.cabin_id, allocation.watts, allocation.batch_id, allocation.seq]
        for allocation in quota.allocation_rows(shift_label)
    ]
    return columns, rows
