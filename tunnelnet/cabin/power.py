"""Per cabin power baseline projection used by the energy quota service."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PowerBaseline:
    """The allocated watts in force for one cabin at a moment."""

    cabin_id: str
    watts: float

    def as_dict(self) -> dict[str, object]:
        return {"cabin_id": self.cabin_id, "watts": self.watts}
