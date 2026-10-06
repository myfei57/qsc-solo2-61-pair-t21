"""Energy quota service with per cabin, per shift, versioned baselines.

Caliber decisions implemented here (see ``shift`` and ``ledger``):

* adjustments take effect at the **next shift** boundary, never mid shift;
* within one ``(cabin, target shift)`` the **later** submission wins, and a
  shift that has already started (carries attributed usage) rejects retargets;
* the same adjustment submitted twice with one idempotency key is applied
  once and returns the first result;
* a multi cabin batch is all or nothing - one invalid cabin rolls the whole
  adjustment back, so no half allocation is ever persisted;
* usage is attributed against the baseline frozen at its shift start and the
  resolved watts are copied onto the usage event, so later adjustments cannot
  rewrite historical attribution or its overage.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from tunnelnet.cabin import PowerBaseline, PowerBook, Registry

from . import usage as usage_module
from .ledger import (
    SEED_SEQ,
    Allocation,
    BaselineLedger,
    Batch,
    MemoryRepository,
    QuotaConflict,
    UsageEvent,
)
from .overage import overage_for
from .report import QuotaReport
from .shift import ShiftCalendar, parse_instant


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QuotaService:
    """Tracks consumption per cabin and compares it with the allocation."""

    def __init__(
        self,
        cabins: Registry,
        book: PowerBook,
        *,
        repository: object | None = None,
        calendar: ShiftCalendar | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._cabins = cabins
        self._book = book
        self._ledger = BaselineLedger(repository if repository is not None else MemoryRepository())
        self._calendar = calendar or ShiftCalendar()
        self._clock = clock
        self._lock = threading.RLock()

    # -- time helpers ------------------------------------------------------

    def _instant(self, at: str | datetime | None) -> datetime:
        if at is None:
            return self._clock()
        if isinstance(at, datetime):
            return at
        try:
            return parse_instant(at)
        except ValueError:
            # Opaque event labels ("t", "now") carry no calendar information;
            # attribute them to the running shift rather than dropping usage.
            return self._clock()

    def current_shift_label(self, at: str | datetime | None = None) -> str:
        return self._calendar.shift_at(self._instant(at)).label

    def next_shift_label(self, at: str | datetime | None = None) -> str:
        return self._calendar.next_shift(self._instant(at)).label

    def calendar(self) -> ShiftCalendar:
        return self._calendar

    # -- seeding -----------------------------------------------------------

    def seed_baseline(
        self,
        cabin_id: str,
        watts: float,
        *,
        shift_label: str = "1970-01-01#0",
        batch_id: str = "seed",
    ) -> Allocation:
        """Establish a cabin's standing baseline, effective from day one."""

        with self._lock:
            existing = self._ledger.winning_allocation(cabin_id, shift_label)
            if existing is not None and existing.batch_id == batch_id:
                return existing
            allocation = Allocation(
                id=f"alloc-{batch_id}-{cabin_id}",
                cabin_id=cabin_id,
                shift_label=shift_label,
                watts=watts,
                batch_id=batch_id,
                seq=SEED_SEQ,
            )
            self._ledger.save_allocation(allocation)
            self._book.set_baseline(cabin_id, watts)
            return allocation

    # -- resolution: the one caliber used by allocation, usage and export ---

    def baseline_watts(self, cabin_id: str, shift_label: str) -> float:
        allocation = self._ledger.winning_allocation(cabin_id, shift_label)
        if allocation is not None:
            return allocation.watts
        return self._book.baseline(cabin_id)

    def load_report(self, shift_label: str = "") -> list[PowerBaseline]:
        target = shift_label or self.current_shift_label()
        return [
            PowerBaseline(cabin_id=cabin.id, watts=self.baseline_watts(cabin.id, target))
            for cabin in self._cabins.active()
        ]

    # -- adjustment submission ---------------------------------------------

    def submit_allocation(
        self,
        items: dict[str, float] | list[tuple[str, float]],
        *,
        idempotency_key: str,
        at: str | datetime | None = None,
        requested_by: str = "operator",
        target_shift: str = "",
    ) -> Batch:
        """Submit one baseline adjustment, effective at the next shift.

        Returns the committed batch. Replaying the same ``idempotency_key``
        returns the original batch without applying anything a second time.
        """

        normalized = list(items.items()) if isinstance(items, dict) else list(items)
        with self._lock:
            replay = self._ledger.find_idempotent(idempotency_key)
            if replay is not None:
                if _payload_signature(replay.items) != _payload_signature(normalized):
                    raise QuotaConflict(
                        f"idempotency key {idempotency_key!r} reused with different cabins/watts"
                    )
                return replay

            instant = self._instant(at)
            shift_label = target_shift or self._calendar.next_shift(instant).label
            target = self._calendar.parse_label(shift_label)
            if self._calendar.has_started(target, instant):
                raise QuotaConflict(
                    f"shift {shift_label} has already started; adjustments apply from the next shift"
                )

            self._validate(normalized, instant)

            seq = self._ledger.next_seq()
            batch_id = f"batch-{seq}-{uuid.uuid4().hex[:8]}"
            batch = Batch(
                id=batch_id,
                idempotency_key=idempotency_key,
                shift_label=shift_label,
                submitted_at=instant.isoformat(),
                requested_by=requested_by,
                seq=seq,
                items=tuple(normalized),
            )
            allocations = [
                Allocation(
                    id=f"alloc-{batch_id}-{cabin_id}",
                    cabin_id=cabin_id,
                    shift_label=shift_label,
                    watts=watts,
                    batch_id=batch_id,
                    seq=seq,
                )
                for cabin_id, watts in normalized
            ]
            # All allocation rows and the batch land together; any failure
            # before this point has written nothing, i.e. a full rollback.
            self._ledger.commit_batch(batch, allocations)
            return batch

    def refresh_baseline(self, watts: float, *, at: str | datetime | None = None) -> list[PowerBaseline]:
        """Give every active cabin the same allocation from the next shift."""

        instant = self._instant(at)
        items = [(cabin.id, watts) for cabin in self._cabins.active()]
        shift_label = self._calendar.next_shift(instant).label
        batch = self.submit_allocation(
            items,
            idempotency_key=f"refresh-{shift_label}-{watts:g}",
            at=instant,
        )
        return [
            PowerBaseline(cabin_id=cabin_id, watts=watts) for cabin_id, watts in batch.items
        ]

    def _validate(self, items: list[tuple[str, float]], instant: datetime) -> None:
        if not items:
            raise QuotaConflict("an allocation batch must contain at least one cabin")
        cabins = [cabin_id for cabin_id, _ in items]
        if len(set(cabins)) != len(cabins):
            raise QuotaConflict("a cabin appears more than once in one allocation batch")
        active = {cabin.id for cabin in self._cabins.active()}
        for cabin_id, watts in items:
            if cabin_id not in active:
                raise QuotaConflict(f"cabin {cabin_id} is not an active cabin")
            if watts < 0:
                raise QuotaConflict(f"baseline for cabin {cabin_id} must not be negative")

    # -- usage attribution -------------------------------------------------

    def add_usage(
        self,
        cabin_id: str,
        watts: float,
        at: str | datetime | None = None,
    ) -> float:
        with self._lock:
            instant = self._instant(at)
            shift_label = self._calendar.shift_at(instant).label
            allocation = self._ledger.winning_allocation(cabin_id, shift_label)
            baseline_watts = allocation.watts if allocation is not None else self._book.baseline(cabin_id)
            batch_id = allocation.batch_id if allocation is not None else "default"
            seq = self._ledger.next_seq()
            event = UsageEvent(
                id=f"usage-{seq}-{uuid.uuid4().hex[:8]}",
                cabin_id=cabin_id,
                shift_label=shift_label,
                watts=watts,
                at=instant.isoformat(),
                baseline_watts=baseline_watts,
                batch_id=batch_id,
                seq=seq,
            )
            self._ledger.save_usage(event)
            return self.usage(cabin_id, shift_label)

    # -- projections (same ledger as the export) ---------------------------

    def shift_usage(self, shift_label: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for event in self._ledger.usage_events(shift_label=shift_label):
            totals[event.cabin_id] = totals.get(event.cabin_id, 0.0) + event.watts
        return totals

    def usage(self, cabin_id: str, shift_label: str = "") -> float:
        target = shift_label or self.current_shift_label()
        return self.shift_usage(target).get(cabin_id, 0.0)

    def usage_by_cabin(self, shift_label: str = "") -> dict[str, float]:
        return dict(self.shift_usage(shift_label or self.current_shift_label()))

    def total_usage(self, shift_label: str = "") -> float:
        with self._lock:
            return usage_module.total(self.shift_usage(shift_label or self.current_shift_label()))

    def peak(self, shift_label: str = "") -> float:
        with self._lock:
            return usage_module.peak(self.shift_usage(shift_label or self.current_shift_label()))

    def ranked_cabin_ids(self, shift_label: str = "") -> list[str]:
        with self._lock:
            return usage_module.rank(self.shift_usage(shift_label or self.current_shift_label()))

    def attributed_usage(self, shift_label: str = "") -> list[UsageEvent]:
        """Raw attributed rows, frozen baseline included; the export source."""

        target = shift_label or self.current_shift_label()
        return self._ledger.usage_events(shift_label=target)

    def allocation_rows(self, shift_label: str = "") -> list[Allocation]:
        """The winning baseline per active cabin for one shift; export source."""

        target = shift_label or self.current_shift_label()
        return [
            row
            for cabin in self._cabins.active()
            if (row := self._ledger.winning_allocation(cabin.id, target)) is not None
        ]

    def batches(self) -> list[Batch]:
        return self._ledger.batches()

    def overage(self, cabin_id: str, shift_label: str = "") -> float:
        target = shift_label or self.current_shift_label()
        return overage_for(self.usage(cabin_id, target), self.baseline_watts(cabin_id, target))

    def allocated(self, shift_label: str = "") -> float:
        target = shift_label or self.current_shift_label()
        return sum(self.baseline_watts(cabin.id, target) for cabin in self._cabins.active())

    def overage_total(self, shift_label: str = "") -> float:
        with self._lock:
            target = shift_label or self.current_shift_label()
            return sum(
                overage_for(watts, self.baseline_watts(cabin_id, target))
                for cabin_id, watts in self.shift_usage(target).items()
            )

    def forecast(self, cabin_id: str, factor: float, shift_label: str = "") -> float:
        return self.usage(cabin_id, shift_label) * factor

    def peak_cabin(self, shift_label: str = "") -> str:
        with self._lock:
            totals = self.shift_usage(shift_label or self.current_shift_label())
            if not totals:
                return ""
            return usage_module.rank(totals)[0]

    def report(self, shift_label: str = "") -> QuotaReport:
        target = shift_label or self.current_shift_label()
        return QuotaReport(
            allocated=self.allocated(target),
            usage_total=self.total_usage(target),
            overage_total=self.overage_total(target),
            peak_cabin=self.peak_cabin(target),
            cabins=len(self._cabins.active()),
            shift_label=target,
        )

    def describe(self) -> str:
        shift_label = self.current_shift_label()
        return (
            f"quota shift={shift_label} allocated={self.allocated(shift_label):.4f} "
            f"usage={self.total_usage(shift_label):.4f} overage={self.overage_total(shift_label):.4f}"
        )


def _payload_signature(items: list[tuple[str, float]] | tuple[tuple[str, float], ...]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((cabin_id, watts) for cabin_id, watts in items))
