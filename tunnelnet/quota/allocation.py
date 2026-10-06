"""Durable, versioned baseline allocation ledger.

The ledger is the single source of truth shared by baseline reports, usage
attribution and the console CSV export. Every adjustment is an immutable
batch of per cabin entries; committing never updates or deletes an existing
entry, so the baseline in force at any past moment stays resolvable forever.

Commits are atomic per request: a client supplied ``idem_key`` either returns
the batch already committed under that key (idempotent retries) or commits one
new batch for every cabin together. A failure for any cabin rolls back the
entries written earlier in the same batch.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass

from tunnelnet.store import Store

from .model import AllocationEntry
from .shifts import EPOCH

ALLOCATION_KIND = "quota_allocation"
BATCH_KIND = "quota_batch"
BOOTSTRAP_KEY = "bootstrap"
BOOTSTRAP_IDEM = "bootstrap-v1"


@dataclass(frozen=True)
class BatchResult:
    """Outcome of a submit call: the committed batch and how it was resolved."""

    batch_id: str
    entries: tuple[AllocationEntry, ...]
    effective_at: str
    idem_hit: bool

    def cabins(self) -> list[str]:
        return [entry.cabin_id for entry in self.entries]


def _manifest_id(idem_key: str) -> str:
    return hashlib.sha256(idem_key.encode("utf-8")).hexdigest()


class AllocationLedger:
    """File backed allocation entries with an in memory resolution index."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._entries: list[AllocationEntry] = []
        self._manifests: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """Load every durable entry and manifest, sweeping orphaned entries."""

        with self._lock:
            entries = [
                AllocationEntry.from_dict(payload)
                for record_id in self._store.list(ALLOCATION_KIND)
                if (payload := self._store.load(ALLOCATION_KIND, record_id)) is not None
            ]
            manifests: dict[str, str] = {}
            committed: set[str] = set()
            for record_id in self._store.list(BATCH_KIND):
                payload = self._store.load(BATCH_KIND, record_id)
                if payload is None:
                    continue
                batch_id = str(payload.get("batch_id", ""))
                manifests[str(payload.get("idem_key", ""))] = batch_id
                committed.update(str(entry_id) for entry_id in payload.get("entry_ids", ()))
            committed_entries: list[AllocationEntry] = []
            for entry in entries:
                if entry.id not in committed:
                    # Left behind by a process that died before committing;
                    # it was never an in-force allocation, so drop it.
                    self._store.delete(ALLOCATION_KIND, entry.id)
                else:
                    committed_entries.append(entry)
            self._entries = sorted(committed_entries, key=_resolution_key)
            self._manifests = manifests

    def bootstrap(self, watts_by_cabin: dict[str, float], at: str = EPOCH) -> BatchResult:
        """Install the opening baseline for every cabin exactly once."""

        if self._entries or self._manifests:
            existing = self._batch_for_key(BOOTSTRAP_IDEM)
            if existing is not None:
                return existing
        return self.commit(watts_by_cabin, submitted_at=at, idem_key=BOOTSTRAP_IDEM, effective_at=at)

    def commit(
        self,
        watts_by_cabin: dict[str, float],
        *,
        submitted_at: str,
        idem_key: str,
        effective_at: str,
    ) -> BatchResult:
        """Commit one batch or return the batch already carrying ``idem_key``."""

        if not idem_key:
            raise ValueError("idem_key is required")
        if not watts_by_cabin:
            raise ValueError("allocation batch is empty")
        with self._lock:
            existing = self._batch_for_key(idem_key)
            if existing is not None:
                return existing

            batch_id = uuid.uuid4().hex
            written: list[str] = []
            entries: list[AllocationEntry] = []
            try:
                for cabin_id, watts in watts_by_cabin.items():
                    entry = AllocationEntry(
                        id=uuid.uuid4().hex,
                        batch_id=batch_id,
                        cabin_id=cabin_id,
                        watts=float(watts),
                        effective_at=effective_at,
                        submitted_at=submitted_at,
                        idem_key=idem_key,
                    )
                    self._store.save(ALLOCATION_KIND, entry.id, entry.as_dict())
                    written.append(entry.id)
                    entries.append(entry)
                manifest = {
                    "batch_id": batch_id,
                    "idem_key": idem_key,
                    "effective_at": effective_at,
                    "submitted_at": submitted_at,
                    "entry_ids": written,
                }
                self._store.save(BATCH_KIND, _manifest_id(idem_key), manifest)
            except BaseException:
                for entry_id in written:
                    self._store.delete(ALLOCATION_KIND, entry_id)
                raise

            self._entries = sorted([*self._entries, *entries], key=_resolution_key)
            self._manifests[idem_key] = batch_id
            return BatchResult(
                batch_id=batch_id,
                entries=tuple(sorted(entries, key=lambda item: item.cabin_id)),
                effective_at=effective_at,
                idem_hit=False,
            )

    def resolve(self, cabin_id: str, at: str) -> AllocationEntry | None:
        """Entry in force for a cabin at a moment, or None before the seed."""

        with self._lock:
            candidates = [
                entry
                for entry in self._entries
                if entry.cabin_id == cabin_id and entry.effective_at <= at
            ]
            return candidates[-1] if candidates else None

    def current(self, cabin_id: str, at: str) -> float:
        entry = self.resolve(cabin_id, at)
        return 0.0 if entry is None else entry.watts

    def entries_for(self, cabin_id: str | None = None) -> list[AllocationEntry]:
        with self._lock:
            if cabin_id is None:
                return list(self._entries)
            return [entry for entry in self._entries if entry.cabin_id == cabin_id]

    def effective_starts(self, cabin_id: str) -> list[str]:
        return sorted({entry.effective_at for entry in self.entries_for(cabin_id)})

    def describe(self) -> str:
        with self._lock:
            cabins = len({entry.cabin_id for entry in self._entries})
            return f"allocations={len(self._entries)} cabins={cabins} batches={len(self._manifests)}"

    def _batch_for_key(self, idem_key: str) -> BatchResult | None:
        batch_id = self._manifests.get(idem_key)
        if batch_id is None:
            return None
        entries = sorted(
            (entry for entry in self._entries if entry.batch_id == batch_id),
            key=lambda item: item.cabin_id,
        )
        effective_at = entries[0].effective_at if entries else ""
        return BatchResult(
            batch_id=batch_id,
            entries=tuple(entries),
            effective_at=effective_at,
            idem_hit=True,
        )


def _resolution_key(entry: AllocationEntry) -> tuple[str, str, str, str]:
    # Applicability order for "latest not after the moment", with the
    # same-cabin same-shift tie break of last submit wins, then entry id.
    return (entry.cabin_id, entry.effective_at, entry.submitted_at, entry.id)
