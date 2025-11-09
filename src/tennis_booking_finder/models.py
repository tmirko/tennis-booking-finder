from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Slot:
    """Represents a single available reservation opportunity."""

    calendar_id: str
    calendar_label: str
    court_id: str
    court_label: str
    start: datetime
    end: datetime
    duration_minutes: int
    price_eur: Optional[float]
    price_code: Optional[str]
    source_url: str
    provider: Optional[str] = None
    sport: str = "tennis"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation of the slot."""

        data: dict[str, object] = {
            "calendar_id": self.calendar_id,
            "calendar_label": self.calendar_label,
            "court_id": self.court_id,
            "court_label": self.court_label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_minutes": self.duration_minutes,
            "price_eur": self.price_eur,
            "price_code": self.price_code,
            "source_url": self.source_url,
        }
        if self.provider:
            data["provider"] = self.provider
        data["sport"] = self.sport
        return data
