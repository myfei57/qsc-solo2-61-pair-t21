"""Durable quota records: versioned baseline allocations and attributed usage."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationEntry:
    """One cabin's baseline watts as of one committed adjustment batch.

    Entries are immutable once committed. ``effective_at`` is the shift start
    from which the watts enter force; resolving the baseline for any moment
    means picking the latest entry not after it, so old reports keep using the
    baseline that was in force at the time.
    """

    id: str
    batch_id: str
    cabin_id: str
    watts: float
    effective_at: str
    submitted_at: str
    idem_key: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "cabin_id": self.cabin_id,
            "watts": self.watts,
            "effective_at": self.effective_at,
            "submitted_at": self.submitted_at,
            "idem_key": self.idem_key,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AllocationEntry":
        return cls(
            id=str(payload.get("id", "")),
            batch_id=str(payload.get("batch_id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            watts=float(payload.get("watts", 0.0)),
            effective_at=str(payload.get("effective_at", "")),
            submitted_at=str(payload.get("submitted_at", "")),
            idem_key=str(payload.get("idem_key", "")),
        )


@dataclass(frozen=True)
class UsageEntry:
    """One consumption event attributed to the baseline in force at its moment.

    ``baseline_watts`` and ``allocation_id`` are a snapshot of the resolved
    allocation: attribution is frozen at record time and can never drift when
    newer adjustments are committed.
    """

    id: str
    cabin_id: str
    watts: float
    at: str
    shift_start: str
    baseline_watts: float
    allocation_id: str

    @property
    def overage(self) -> float:
        return max(0.0, self.watts - self.baseline_watts)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "watts": self.watts,
            "at": self.at,
            "shift_start": self.shift_start,
            "baseline_watts": self.baseline_watts,
            "allocation_id": self.allocation_id,
            "overage": self.overage,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "UsageEntry":
        return cls(
            id=str(payload.get("id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            watts=float(payload.get("watts", 0.0)),
            at=str(payload.get("at", "")),
            shift_start=str(payload.get("shift_start", "")),
            baseline_watts=float(payload.get("baseline_watts", 0.0)),
            allocation_id=str(payload.get("allocation_id", "")),
        )
