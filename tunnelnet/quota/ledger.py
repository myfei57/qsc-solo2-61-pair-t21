"""Durable baseline ledger: the single caliber for allocation and attribution.

Three record kinds share one repository so that baseline allocation, usage
attribution and the console export all read the same numbers:

* ``quota_batch``      - one submitted adjustment (the idempotency envelope)
* ``quota_allocation`` - one cabin's watts for one shift (immutable once won)
* ``quota_usage``      - one attributed usage event with its frozen baseline

An allocation record is the append-only outcome of a batch for a given
``(cabin, shift)``. Allocations never move or get overwritten: a newer batch
that wins the same cell writes a fresh allocation document under a fresh id,
so the history of which batch the shift was charged to is preserved while the
winning row is the one with the greatest ``seq``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

BATCH_KIND = "quota_batch"
ALLOCATION_KIND = "quota_allocation"
USAGE_KIND = "quota_usage"

# Baselines established at seeding time sort before every real adjustment
# because their seq is zero and real submissions start at one.
SEED_SEQ = 0
FIRST_SEQ = 1


class QuotaConflict(Exception):
    """Raised when a batch violates the next-shift or idempotency contract."""


@dataclass(frozen=True)
class Allocation:
    """The watts one cabin is allowed for one shift."""

    id: str
    cabin_id: str
    shift_label: str
    watts: float
    batch_id: str
    seq: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "shift_label": self.shift_label,
            "watts": self.watts,
            "batch_id": self.batch_id,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Allocation":
        return cls(
            id=str(payload["id"]),
            cabin_id=str(payload["cabin_id"]),
            shift_label=str(payload["shift_label"]),
            watts=float(payload["watts"]),
            batch_id=str(payload["batch_id"]),
            seq=int(payload["seq"]),
        )


@dataclass(frozen=True)
class Batch:
    """One submitted, idempotent baseline adjustment."""

    id: str
    idempotency_key: str
    shift_label: str
    submitted_at: str
    requested_by: str
    seq: int
    items: tuple[tuple[str, float], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "shift_label": self.shift_label,
            "submitted_at": self.submitted_at,
            "requested_by": self.requested_by,
            "seq": self.seq,
            "items": [{"cabin_id": cabin_id, "watts": watts} for cabin_id, watts in self.items],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Batch":
        raw_items = payload.get("items", [])
        items = tuple(
            (str(item["cabin_id"]), float(item["watts"]))
            for item in raw_items  # type: ignore[union-attr]
        )
        return cls(
            id=str(payload["id"]),
            idempotency_key=str(payload["idempotency_key"]),
            shift_label=str(payload["shift_label"]),
            submitted_at=str(payload["submitted_at"]),
            requested_by=str(payload.get("requested_by", "")),
            seq=int(payload["seq"]),
            items=items,
        )


@dataclass(frozen=True)
class UsageEvent:
    """Usage charged against the baseline that was frozen for its shift."""

    id: str
    cabin_id: str
    shift_label: str
    watts: float
    at: str
    baseline_watts: float
    batch_id: str
    seq: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "shift_label": self.shift_label,
            "watts": self.watts,
            "at": self.at,
            "baseline_watts": self.baseline_watts,
            "batch_id": self.batch_id,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "UsageEvent":
        return cls(
            id=str(payload["id"]),
            cabin_id=str(payload["cabin_id"]),
            shift_label=str(payload["shift_label"]),
            watts=float(payload["watts"]),
            at=str(payload["at"]),
            baseline_watts=float(payload["baseline_watts"]),
            batch_id=str(payload["batch_id"]),
            seq=int(payload["seq"]),
        )


class MemoryRepository:
    """In process repository with the same interface as the file backed store.

    Used by tests and by processes that run without a store directory.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, dict[str, object]]] = {}
        self.lock = threading.RLock()

    def save(self, kind: str, record_id: str, value: object) -> None:
        with self.lock:
            self._records.setdefault(kind, {})[record_id] = dict(value)  # type: ignore[arg-type]

    def load(self, kind: str, record_id: str) -> dict[str, object] | None:
        with self.lock:
            doc = self._records.get(kind, {}).get(record_id)
            return None if doc is None else dict(doc)

    def exists(self, kind: str, record_id: str) -> bool:
        with self.lock:
            return record_id in self._records.get(kind, {})

    def list(self, kind: str) -> list[str]:
        with self.lock:
            return sorted(self._records.get(kind, {}))


class BaselineLedger:
    """Append only, repository backed journal of baselines and attributed usage."""

    def __init__(self, repository: object) -> None:
        self._repo = repository
        self._lock = threading.RLock()

    # -- batches -----------------------------------------------------------

    def batch_exists(self, batch_id: str) -> bool:
        return bool(self._repo.exists(BATCH_KIND, batch_id))  # type: ignore[attr-defined]

    def find_idempotent(self, idempotency_key: str) -> Batch | None:
        for batch in self.batches():
            if batch.idempotency_key == idempotency_key:
                return batch
        return None

    def batches(self) -> list[Batch]:
        docs = [self._repo.load(BATCH_KIND, record_id) for record_id in self._repo.list(BATCH_KIND)]  # type: ignore[attr-defined]
        return [Batch.from_dict(doc) for doc in docs if doc is not None]

    def next_seq(self) -> int:
        """Monotonic submission number; later submissions always win."""

        highest = SEED_SEQ
        for batch in self.batches():
            highest = max(highest, batch.seq)
        for event in self.usage_events():
            highest = max(highest, event.seq)
        return highest + 1

    # -- allocations -------------------------------------------------------

    def save_allocation(self, allocation: Allocation) -> None:
        self._repo.save(ALLOCATION_KIND, allocation.id, allocation.as_dict())  # type: ignore[attr-defined]

    def allocations(self) -> list[Allocation]:
        docs = [
            self._repo.load(ALLOCATION_KIND, record_id)  # type: ignore[attr-defined]
            for record_id in self._repo.list(ALLOCATION_KIND)  # type: ignore[attr-defined]
        ]
        return [Allocation.from_dict(doc) for doc in docs if doc is not None]

    def allocations_for(self, cabin_id: str, shift_label: str) -> list[Allocation]:
        return [
            row
            for row in self.allocations()
            if row.cabin_id == cabin_id and row.shift_label == shift_label
        ]

    def winning_allocation(self, cabin_id: str, shift_label: str) -> Allocation | None:
        """The allocation that governs a shift: greatest label at/below it.

        Baselines carry forward across shifts, so a shift without its own
        allocation inherits the most recent earlier one. Ties on label are
        broken by seq (last write wins).
        """

        candidates = [
            row for row in self.allocations() if row.cabin_id == cabin_id and row.shift_label <= shift_label
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: (row.shift_label, row.seq))

    # -- usage -------------------------------------------------------------

    def save_usage(self, event: UsageEvent) -> None:
        self._repo.save(USAGE_KIND, event.id, event.as_dict())  # type: ignore[attr-defined]

    def usage_events(self, cabin_id: str = "", shift_label: str = "") -> list[UsageEvent]:
        docs = [
            self._repo.load(USAGE_KIND, record_id)  # type: ignore[attr-defined]
            for record_id in self._repo.list(USAGE_KIND)  # type: ignore[attr-defined]
        ]
        events = [UsageEvent.from_dict(doc) for doc in docs if doc is not None]
        if cabin_id:
            events = [event for event in events if event.cabin_id == cabin_id]
        if shift_label:
            events = [event for event in events if event.shift_label == shift_label]
        return events

    # -- atomic submission -------------------------------------------------

    def commit_batch(
        self,
        batch: Batch,
        allocations: list[Allocation],
        *,
        idempotent_replay: bool = False,
    ) -> None:
        """Persist a batch and its allocation rows together or not at all.

        ``idempotent_replay`` short-circuits: the first submission's rows are
        already durable, so the replay returns without writing anything.
        """

        with self._lock:
            if idempotent_replay:
                return
            for allocation in allocations:
                self.save_allocation(allocation)
            self._repo.save(BATCH_KIND, batch.id, batch.as_dict())  # type: ignore[attr-defined]
