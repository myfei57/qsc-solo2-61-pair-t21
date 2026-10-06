"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tunnelnet.audit import AuditRegistry
from tunnelnet.store import Store

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from tunnelnet.console.telemetry import Telemetry
    from tunnelnet.quota import QuotaService

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


def quota_export(quota: "QuotaService") -> tuple[list[str], list[list[object]]]:
    """Allocation versions and usage reconciled per shift, one shared basis.

    Every usage row carries the exact allocation id and baseline watts it was
    attributed to, so the export can always be tied back to one allocation
    record and historical rows never change after later adjustments.
    """

    columns = [
        "record",
        "cabin_id",
        "shift_start",
        "shift_name",
        "watts",
        "baseline_watts",
        "overage",
        "allocation_id",
        "batch_id",
        "submitted_at",
        "effective_at",
    ]
    rows: list[list[object]] = []
    for entry in quota.allocation_versions():
        rows.append(
            [
                "allocation",
                entry["cabin_id"],
                "",
                "",
                entry["watts"],
                "",
                "",
                entry["id"],
                entry["batch_id"],
                entry["submitted_at"],
                entry["effective_at"],
            ]
        )
    for row in quota.attribution():
        rows.append(
            [
                "usage",
                row.cabin_id,
                row.shift.start,
                row.shift.name,
                row.watts,
                row.baseline_watts,
                row.overage,
                row.allocation_id,
                "",
                "",
                "",
            ]
        )
    return columns, rows
