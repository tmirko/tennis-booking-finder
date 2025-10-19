from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

import requests
from zoneinfo import ZoneInfo

from ..models import Slot
from . import eversports, ltm


def collect_slots(
    *,
    session: requests.Session,
    pages: int,
    timezone: ZoneInfo,
    timeout: int,
    dates: Sequence[date] | None = None,
) -> list[Slot]:
    slots: list[Slot] = []
    slots.extend(
        ltm.fetch_slots(
            session=session,
            pages=pages,
            timezone=timezone,
            timeout=timeout,
        )
    )

    target_dates: Sequence[date] | None = dates
    if target_dates is None:
        today = datetime.now(timezone).date()
        horizon_days = max(1, min(pages * 4, 14))
        target_dates = [today + timedelta(days=offset) for offset in range(horizon_days)]

    slots.extend(
        eversports.fetch_slots(
            timezone=timezone,
            timeout=timeout,
            dates=target_dates,
        )
    )

    now = datetime.now(timezone)
    return [slot for slot in slots if slot.end > now]
