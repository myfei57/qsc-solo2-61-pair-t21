"""Baseline projections derived from the versioned quota ledger."""

from __future__ import annotations

from tunnelnet.cabin import PowerBaseline

from .service import QuotaService


def baseline_report(service: QuotaService, shift_label: str = "") -> list[PowerBaseline]:
    """Winning allocated watts for every active cabin at one shift."""

    return service.load_report(shift_label)
