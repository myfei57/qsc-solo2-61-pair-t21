"""Baseline projections derived from the durable allocation ledger."""

from __future__ import annotations

from tunnelnet.cabin import PowerBaseline, Registry

from .allocation import AllocationLedger


def baseline_report(cabins: Registry, ledger: AllocationLedger, at: str) -> list[PowerBaseline]:
    """Watts in force at ``at`` for every active cabin, in registry order."""

    return [
        PowerBaseline(cabin_id=cabin.id, watts=ledger.current(cabin.id, at))
        for cabin in cabins.active()
    ]
