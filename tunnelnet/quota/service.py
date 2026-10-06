"""Energy quota service bound to durable allocations and usage.

Baselines are allocated per cabin and per shift. A submit only becomes the
in force allocation at the next shift start (``policy`` documents the choice), and
usage is attributed when it happens against the allocation in force at that
exact moment, so historical reports always reconcile with the baseline of the
time.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from tunnelnet.cabin import PowerBaseline, Registry

from . import usage as usage_module
from .allocation import AllocationLedger, BatchResult
from .attribution import ShiftAttribution, attribute, totals
from .baseline import baseline_report
from .journal import UsageJournal
from .model import UsageEntry
from .overage import overage_for
from .report import QuotaReport
from .shifts import Shift, ShiftError, containing, next_start, parse, shift_for_effective

# Submitted adjustments apply to new consumption only, from the next shift
# boundary on. A shift therefore keeps one constant baseline for its whole
# duration and cross-shift reports never mix two bases.
EFFECTIVE_POLICY = "next_shift"
# Two adjustments to one cabin landing on the same effective shift: the later
# submission wins; both records stay on file for the audit trail.
SAME_DAY_POLICY = "last_submit_wins"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AdjustmentError(ValueError):
    """A batch adjustment failed validation; nothing is committed."""


class QuotaService:
    """Tracks consumption per cabin and reconciles it with allocations."""

    def __init__(
        self,
        cabins: Registry,
        ledger: AllocationLedger,
        journal: UsageJournal,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._cabins = cabins
        self._ledger = ledger
        self._journal = journal
        self._clock = clock
        self._lock = threading.RLock()

    def policy(self) -> dict[str, str]:
        return {"effective": EFFECTIVE_POLICY, "same_day": SAME_DAY_POLICY}

    def refresh_baseline(self, watts: float, at: str = "") -> list[PowerBaseline]:
        """Allocate the same watts to every active cabin from next shift on."""

        moment = at or self._clock()
        cabins = {cabin.id: watts for cabin in self._cabins.active()}
        self.submit_adjustment(cabins, submitted_at=moment, idem_key=f"refresh:{moment}:{watts:g}")
        return self.load_report(moment)

    def submit_adjustment(
        self,
        watts_by_cabin: dict[str, float],
        *,
        submitted_at: str = "",
        idem_key: str = "",
        effective_at: str = "",
    ) -> BatchResult:
        """Commit a per cabin allocation batch atomically.

        Validates the whole batch first; on any failure nothing is written and
        a repeat with the same ``idem_key`` returns the original batch.
        """

        moment = submitted_at or self._clock()
        parse(moment)
        if not idem_key:
            raise AdjustmentError("idem_key is required")
        if not watts_by_cabin:
            raise AdjustmentError("adjustment allocates no cabins")
        if len(watts_by_cabin) != len(set(watts_by_cabin)):
            raise AdjustmentError("cabin listed more than once in one batch")

        active = {cabin.id for cabin in self._cabins.active()}
        watts_clean: dict[str, float] = {}
        for cabin_id, watts in watts_by_cabin.items():
            if cabin_id not in active:
                raise AdjustmentError(f"cabin {cabin_id} is not active")
            try:
                value = float(watts)
            except (TypeError, ValueError) as exc:
                raise AdjustmentError(f"watts for {cabin_id} must be a number") from exc
            if value < 0:
                raise AdjustmentError(f"watts for {cabin_id} must be non-negative")
            watts_clean[cabin_id] = value

        target = effective_at or next_start(moment)
        try:
            shift_for_effective(target)
        except ShiftError as exc:
            raise AdjustmentError(str(exc)) from exc
        if target <= moment:
            raise AdjustmentError("effective time must be a future shift start")

        return self._ledger.commit(
            watts_clean, submitted_at=moment, idem_key=idem_key, effective_at=target
        )

    def bootstrap(self, watts_by_cabin: dict[str, float], at: str) -> BatchResult:
        """Seed the opening baseline; idempotent across restarts."""

        return self._ledger.bootstrap(watts_by_cabin, at=at)

    def load_report(self, at: str = "") -> list[PowerBaseline]:
        return baseline_report(self._cabins, self._ledger, at or self._clock())

    def allocation_versions(self, cabin_id: str = "") -> list[dict[str, object]]:
        return [entry.as_dict() for entry in self._ledger.entries_for(cabin_id or None)]

    def add_usage(self, cabin_id: str, watts: float, at: str = "", idem_key: str = "") -> float:
        """Record one event and attribute it to the baseline in force then."""

        entry = self._journal.record(cabin_id, watts, at or self._clock(), idem_key=idem_key)
        return self.usage(cabin_id, entry.shift_start)

    def add_usage_event(
        self, cabin_id: str, watts: float, at: str = "", idem_key: str = ""
    ) -> UsageEntry:
        return self._journal.record(cabin_id, watts, at or self._clock(), idem_key=idem_key)

    def _rows(self, cabin_id: str = "", shift_start: str = "") -> list[ShiftAttribution]:
        return attribute(self._journal.entries(cabin_id, shift_start))

    def usage(self, cabin_id: str, shift_start: str = "") -> float:
        return sum(row.watts for row in self._rows(cabin_id, shift_start))

    def total_usage(self, shift_start: str = "") -> float:
        rows = self._rows(shift_start=shift_start)
        return usage_module.total({row.cabin_id: row.watts for row in rows}) if rows else 0.0

    def peak(self, shift_start: str = "") -> float:
        rows = self._rows(shift_start=shift_start)
        return max((row.watts for row in rows), default=0.0)

    def usage_by_cabin(self, shift_start: str = "") -> dict[str, float]:
        totals_by_cabin: dict[str, float] = {}
        for row in self._rows(shift_start=shift_start):
            totals_by_cabin[row.cabin_id] = totals_by_cabin.get(row.cabin_id, 0.0) + row.watts
        return totals_by_cabin

    def ranked_cabin_ids(self, shift_start: str = "") -> list[str]:
        return usage_module.rank(self.usage_by_cabin(shift_start))

    def overage(self, cabin_id: str, shift_start: str = "") -> float:
        return sum(
            row.overage for row in self._rows(cabin_id, shift_start) if row.cabin_id == cabin_id
        )

    def allocated(self, at: str = "") -> float:
        moment = at or self._clock()
        return sum(self._ledger.current(cabin.id, moment) for cabin in self._cabins.active())

    def overage_total(self, shift_start: str = "") -> float:
        return sum(row.overage for row in self._rows(shift_start=shift_start))

    def forecast(self, cabin_id: str, factor: float) -> float:
        return self.usage(cabin_id) * factor

    def peak_cabin(self, shift_start: str = "") -> str:
        ranked = self.ranked_cabin_ids(shift_start)
        return ranked[0] if ranked else ""

    def current_shift(self, at: str = "") -> Shift:
        return containing(at or self._clock())

    def attribution(self, cabin_id: str = "", shift_start: str = "") -> list[ShiftAttribution]:
        return self._rows(cabin_id, shift_start)

    def events(self, cabin_id: str = "", shift_start: str = "") -> list[UsageEntry]:
        return self._journal.entries(cabin_id, shift_start)

    def history(self, cabin_id: str = "") -> list[ShiftAttribution]:
        """Every shift that has consumption, reconciled with its own baseline."""

        return self._rows(cabin_id)

    def report(self) -> QuotaReport:
        shift = self.current_shift()
        rows = self._rows(shift_start=shift.start)
        usage_total, overage_total = totals(rows)
        return QuotaReport(
            allocated=self.allocated(),
            usage_total=usage_total,
            overage_total=overage_total,
            peak_cabin=self.peak_cabin(shift.start),
            cabins=len(self._cabins.active()),
            shift=shift.as_dict(),
            policy=self.policy(),
        )

    def describe(self) -> str:
        shift = self.current_shift()
        return (
            f"quota shift={shift.name} allocated={self.allocated():.4f} "
            f"usage={self.total_usage(shift.start):.4f} "
            f"overage={self.overage_total(shift.start):.4f} "
            f"events={self._journal.count()}"
        )
