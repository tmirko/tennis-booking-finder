from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Sequence

import cloudscraper
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from ..models import Slot

BOOKING_PAGE_URL = "https://www.eversports.at/sb/vienna-sporthotel"
CALENDAR_ENDPOINT = "https://www.eversports.at/api/booking/calendar/update"
FACILITY_ID = "12886"
FACILITY_SLUG = "vienna-sporthotel"
SPORT_META = {
    "id": "433",
    "slug": "tennis",
    "name": "Tennis",
    "uuid": "b38729e9-69de-11e8-bdc6-02bd505aa7b2",
}
PROVIDER = "eversports"
AVAILABLE_STATES = {"free", "open"}
BUSY_TOKENS = {
    "occupied",
    "booked",
    "blocked",
    "reserved",
    "besetzt",
    "belegt",
    "geschlossen",
    "abo",
}


def fetch_slots(
    *,
    timezone: ZoneInfo,
    timeout: int,
    dates: Sequence[date] | None = None,
) -> Iterable[Slot]:
    target_dates = list(dict.fromkeys(dates or [datetime.now(timezone).date()]))
    scraper = cloudscraper.create_scraper()
    slots: list[Slot] = []
    seen: set[tuple[str, str, datetime]] = set()

    for current_date in target_dates:
        html = _fetch_calendar_html(scraper, current_date, timeout)
        soup = BeautifulSoup(html, "html.parser")
        court_ids = _extract_court_ids(soup)
        blocked = _fetch_blocked_slots(
            scraper,
            current_date,
            court_ids,
            timeout,
        )
        for slot in _parse_calendar_html(soup, current_date, timezone, blocked):
            key = (slot.calendar_id, slot.court_id, slot.start)
            if key in seen:
                continue
            seen.add(key)
            slots.append(slot)

    return slots


def _fetch_calendar_html(scraper: cloudscraper.CloudScraper, target_date: date, timeout: int) -> str:
    data = {
        "facilityId": FACILITY_ID,
        "facilitySlug": FACILITY_SLUG,
        "sport[id]": SPORT_META["id"],
        "sport[slug]": SPORT_META["slug"],
        "sport[name]": SPORT_META["name"],
        "sport[uuid]": SPORT_META["uuid"],
        "date": target_date.isoformat(),
        "type": "user",
    }
    response = scraper.post(CALENDAR_ENDPOINT, data=data, timeout=timeout)
    response.raise_for_status()
    return response.text


def _parse_calendar_html(
    soup: BeautifulSoup | str,
    fallback_date: date,
    timezone: ZoneInfo,
    blocked_slots: set[tuple[str, int, str]] | None = None,
) -> Iterator[Slot]:
    if isinstance(soup, str):
        soup = BeautifulSoup(soup, "html.parser")

    blocked_slots = blocked_slots or set()
    for day_block in soup.select("tbody[data-date]"):
        day_str = day_block.get("data-date") or fallback_date.isoformat()
        try:
            day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            day_date = fallback_date

        for court_row in day_block.select("tr.court"):
            header_cell = court_row.find("td")
            if not header_cell:
                continue
            court_label = header_cell.get_text(strip=True)
            court_id = header_cell.get("data-court", "")
            court_uuid = header_cell.get("data-court-uuid", "")
            calendar_label = court_row.get("data-area", "Vienna Sporthotel")

            court_key = court_id or court_uuid or court_label
            candidates: dict[tuple[str, str, str], tuple] = {}
            blocked: set[tuple[str, str, str]] = set()

            for slot_cell in court_row.select("td[data-state]"):
                key = (
                    court_key,
                    slot_cell.get("data-start", ""),
                    slot_cell.get("data-end", ""),
                )

                state = (slot_cell.get("data-state") or "").strip().lower()
                tooltip_text = (
                    slot_cell.get("data-original-title")
                    or slot_cell.get("title")
                    or slot_cell.get("aria-label")
                    or ""
                ).strip().lower()

                is_busy_state = bool(state and state not in AVAILABLE_STATES)
                is_busy_tooltip = any(token in tooltip_text for token in BUSY_TOKENS)

                if is_busy_state or is_busy_tooltip:
                    blocked.add(key)
                    candidates.pop(key, None)
                    continue

                if key in blocked or key in candidates:
                    continue

                candidates[key] = slot_cell

            for (_, start_raw, end_raw), slot_cell in candidates.items():
                if _is_blocked(blocked_slots, day_date, court_key, start_raw, end_raw):
                    continue
                slot = _build_slot(
                    slot_cell,
                    day_date=day_date,
                    timezone=timezone,
                    court_label=court_label,
                    court_id=court_id or court_uuid,
                    calendar_label=calendar_label,
                )
                if slot:
                    yield slot


def _build_slot(
    slot_cell,
    *,
    day_date: date,
    timezone: ZoneInfo,
    court_label: str,
    court_id: str,
    calendar_label: str,
) -> Slot | None:
    state = (slot_cell.get("data-state") or "").strip().lower()
    if state not in AVAILABLE_STATES:
        return None

    tooltip_text = (
        slot_cell.get("data-original-title")
        or slot_cell.get("title")
        or slot_cell.get("aria-label")
        or ""
    ).strip().lower()

    if tooltip_text.startswith("occupied"):
        return None

    if tooltip_text and not any(
        token in tooltip_text for token in ("free", "frei", "open")
    ):
        return None

    start_raw = slot_cell.get("data-start")
    end_raw = slot_cell.get("data-end")
    price_raw = slot_cell.get("data-price")

    if not start_raw or not end_raw:
        return None

    try:
        start_time = datetime.strptime(start_raw, "%H%M").time()
        end_time = datetime.strptime(end_raw, "%H%M").time()
    except ValueError:
        return None

    start_dt = datetime.combine(day_date, start_time, timezone)
    end_dt = datetime.combine(day_date, end_time, timezone)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    duration_minutes = int((end_dt - start_dt).total_seconds() // 60)
    price_value = None
    if price_raw:
        try:
            price_value = float(price_raw.replace(",", "."))
        except ValueError:
            price_value = None

    price_code = slot_cell.get("data-rate")

    if slot_cell.get("data-open") not in {"data-open", "true"}:
        return None

    if not price_value and tooltip_text:
        if not any(token in tooltip_text for token in ("free", "frei", "open")):
            return None

    return Slot(
        calendar_id=FACILITY_ID,
        calendar_label=calendar_label or "Vienna Sporthotel",
        court_id=court_id,
        court_label=court_label,
        start=start_dt,
        end=end_dt,
        duration_minutes=duration_minutes,
        price_eur=price_value,
        price_code=price_code,
        source_url=BOOKING_PAGE_URL,
        provider=PROVIDER,
    )


def _extract_court_ids(soup: BeautifulSoup) -> set[str]:
    court_ids: set[str] = set()
    for cell in soup.select("tr.court td[data-court], tr.court td[data-court-uuid]"):
        value = cell.get("data-court") or cell.get("data-court-uuid")
        if value:
            court_ids.add(value)
    return court_ids


def _time_str_to_minutes(raw: str) -> int | None:
    if not raw or len(raw) != 4:
        return None
    try:
        hours = int(raw[:2])
        minutes = int(raw[2:])
    except ValueError:
        return None
    return hours * 60 + minutes


def _fetch_blocked_slots(
    scraper: cloudscraper.CloudScraper,
    start_date: date,
    court_ids: set[str],
    timeout: int,
) -> set[tuple[str, int, str]]:
    if not court_ids:
        return set()

    params: list[tuple[str, str]] = [
        ("facilityId", FACILITY_ID),
        ("startDate", start_date.isoformat()),
    ]
    for court in sorted(court_ids):
        params.append(("courts[]", court))

    try:
        response = scraper.get(
            "https://www.eversports.at/api/slot",
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return set()

    blocked: set[tuple[str, int, str]] = set()
    for entry in payload.get("slots", []):
        start_minutes = _time_str_to_minutes(entry.get("start", ""))
        court_raw = entry.get("court")
        date_str = entry.get("date")

        if start_minutes is None or court_raw is None or not date_str:
            continue

        blocked.add((date_str, start_minutes, str(court_raw)))

    return blocked


def _is_blocked(
    blocked: set[tuple[str, int, str]],
    day_date: date,
    court_key: str,
    start_raw: str,
    end_raw: str,
) -> bool:
    start_minutes = _time_str_to_minutes(start_raw)
    end_minutes = _time_str_to_minutes(end_raw)

    if start_minutes is None or end_minutes is None:
        return False

    day_key = day_date.isoformat()
    current = start_minutes
    while current < end_minutes:
        if (day_key, current, court_key) in blocked:
            return True
        current += 30

    return False
