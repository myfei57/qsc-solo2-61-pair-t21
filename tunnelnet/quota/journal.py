"""Durable usage journal; every event is attributed when it first happens."""

from __future__ import annotations

import hashlib
import threading
import uuid

from tunnelnet.store import Store

from .allocation import AllocationLedger
from .model import UsageEntry
from .shifts import containing

USAGE_KIND = "quota_usage"
EVENT_KEY_KIND = "quota_usage_key"


class UsageJournal:
    """Append only consumption events with their baseline snapshot."""

    def __init__(self, store: Store, allocations: AllocationLedger) -> None:
        self._store = store
        self._allocations = allocations
        self._lock = threading.RLock()
        self._entries: list[UsageEntry] = []
        self._idem: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            entries = [
                UsageEntry.from_dict(payload)
                for record_id in self._store.list(USAGE_KIND)
                if (payload := self._store.load(USAGE_KIND, record_id)) is not None
            ]
            by_id = {entry.id: entry for entry in entries}
            idem: dict[str, str] = {}
            for record_id in self._store.list(EVENT_KEY_KIND):
                payload = self._store.load(EVENT_KEY_KIND, record_id)
                if payload is None:
                    continue
                event_id = str(payload.get("event_id", ""))
                if event_id in by_id:
                    idem[str(payload.get("idem_key", ""))] = event_id
            self._entries = sorted(entries, key=lambda entry: (entry.at, entry.id))
            self._idem = idem

    def record(self, cabin_id: str, watts: float, at: str, idem_key: str = "") -> UsageEntry:
        """Attribute one event against the allocation in force at ``at``."""

        if not cabin_id:
            raise ValueError("cabin_id is required")
        if watts < 0:
            raise ValueError("watts must be non-negative")
        shift = containing(at)
        with self._lock:
            if idem_key:
                event_id = self._idem.get(idem_key)
                if event_id is not None:
                    return next(entry for entry in self._entries if entry.id == event_id)
            allocation = self._allocations.resolve(cabin_id, at)
            entry = UsageEntry(
                id=uuid.uuid4().hex,
                cabin_id=cabin_id,
                watts=float(watts),
                at=at,
                shift_start=shift.start,
                baseline_watts=0.0 if allocation is None else allocation.watts,
                allocation_id="" if allocation is None else allocation.id,
            )
            self._store.save(USAGE_KIND, entry.id, entry.as_dict())
            if idem_key:
                key_id = hashlib.sha256(idem_key.encode("utf-8")).hexdigest()
                self._store.save(
                    EVENT_KEY_KIND,
                    key_id,
                    {"idem_key": idem_key, "event_id": entry.id},
                )
                self._idem[idem_key] = entry.id
            self._entries.append(entry)
            self._entries.sort(key=lambda item: (item.at, item.id))
        return entry

    def entries(self, cabin_id: str = "", shift_start: str = "") -> list[UsageEntry]:
        with self._lock:
            return [
                entry
                for entry in self._entries
                if (not cabin_id or entry.cabin_id == cabin_id)
                and (not shift_start or entry.shift_start == shift_start)
            ]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def describe(self) -> str:
        return f"usage_events={self.count()}"
