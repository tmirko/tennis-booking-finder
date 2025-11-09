from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Sequence

import cloudscraper
import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from ..models import Slot

logger = logging.getLogger(__name__)

CALENDAR_ENDPOINT = "https://www.eversports.at/api/booking/calendar/update"


@dataclass(frozen=True)
class SportMeta:
    id: str
    slug: str
    name: str
    uuid: str


@dataclass(frozen=True)
class FacilityConfig:
    id: str
    slug: str
    label: str
    booking_url: str
    sports: tuple[SportMeta, ...]


FACILITIES: tuple[FacilityConfig, ...] = (
    FacilityConfig(
        id="12886",
        slug="vienna-sporthotel",
        label="Vienna Sporthotel",
        booking_url="https://www.eversports.at/sb/vienna-sporthotel",
        sports=(
            SportMeta(
                id="433",
                slug="tennis",
                name="Tennis",
                uuid="b38729e9-69de-11e8-bdc6-02bd505aa7b2",
            ),
        ),
    ),
    FacilityConfig(
        id="12782",
        slug="tennis-point-vienna-ej5tqupn",
        label="Tennis Point Vienna",
        booking_url="https://www.eversports.at/sb/tennis-point-vienna-ej5tqupn",
        sports=(
            SportMeta(
                id="433",
                slug="tennis",
                name="Tennis",
                uuid="b38729e9-69de-11e8-bdc6-02bd505aa7b2",
            ),
        ),
    ),
    FacilityConfig(
        id="80214",
        slug="kultur-und-sportvereinigung-der-wiener-gemeindebediensteten",
        label="KSV Wiener Gemeindebedienstete",
        booking_url="https://www.eversports.at/sb/kultur-und-sportvereinigung-der-wiener-gemeindebediensteten",
        sports=(
            SportMeta(
                id="1747",
                slug="tennis-outdoor",
                name="Tennis outdoor",
                uuid="b389170d-69de-11e8-bdc6-02bd505aa7b2",
            ),
            SportMeta(
                id="1748",
                slug="tennis-indoor",
                name="Tennis indoor",
                uuid="b38917a8-69de-11e8-bdc6-02bd505aa7b2",
            ),
        ),
    ),
)

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
    # Configure cloudscraper with realistic browser headers to avoid 403 errors
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    # Add additional headers to make requests look more like a real browser
    scraper.headers.update({
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Cache-Control': 'max-age=0',
    })
    slots: list[Slot] = []
    seen: set[tuple[str, str, datetime]] = set()
    errors: list[str] = []
    
    # Track which facilities we've initialized (visited their booking page)
    initialized_facilities: set[str] = set()

    for facility in FACILITIES:
        # Initialize session by visiting the booking page first (establishes cookies/session)
        if facility.id not in initialized_facilities:
            try:
                logger.debug(f"Initializing session for {facility.label} by visiting booking page")
                # First visit the main page to establish initial session
                try:
                    scraper.get(
                        'https://www.eversports.at/',
                        timeout=timeout,
                        headers={
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
                        }
                    )
                    time.sleep(0.5)
                except Exception:
                    pass  # Continue anyway
                
                # Then visit the specific facility booking page
                init_response = scraper.get(
                    facility.booking_url,
                    timeout=timeout,
                    headers={
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
                        'Referer': 'https://www.eversports.at/',
                    }
                )
                init_response.raise_for_status()
                initialized_facilities.add(facility.id)
                # Small delay after initialization to let session settle
                time.sleep(1.5)
            except Exception as init_e:
                logger.warning(f"Failed to initialize session for {facility.label}: {init_e}")
                # Continue anyway, might still work
        
        for current_date in target_dates:
            for sport in facility.sports:
                max_retries = 2
                retry_count = 0
                success = False
                
                while retry_count <= max_retries and not success:
                    try:
                        if retry_count > 0:
                            # Wait longer between retries
                            wait_time = 2 * retry_count
                            logger.debug(f"Retrying {facility.label} (attempt {retry_count + 1}/{max_retries + 1}) after {wait_time}s")
                            time.sleep(wait_time)
                            
                            # Re-initialize session on retry
                            try:
                                # First visit main page
                                scraper.get(
                                    'https://www.eversports.at/',
                                    timeout=timeout,
                                    headers={
                                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                                        'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
                                    }
                                )
                                time.sleep(0.5)
                                # Then visit facility page
                                init_response = scraper.get(
                                    facility.booking_url,
                                    timeout=timeout,
                                    headers={
                                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                                        'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
                                        'Referer': 'https://www.eversports.at/',
                                    }
                                )
                                init_response.raise_for_status()
                                time.sleep(1.5)
                            except Exception:
                                pass  # Continue with retry anyway
                        
                        html = _fetch_calendar_html(
                            scraper,
                            facility=facility,
                            sport=sport,
                            target_date=current_date,
                            timeout=timeout,
                        )
                        soup = BeautifulSoup(html, "html.parser")
                        court_ids = _extract_court_ids(soup)
                        blocked = _fetch_blocked_slots(
                            scraper,
                            facility=facility,
                            start_date=current_date,
                            court_ids=court_ids,
                            timeout=timeout,
                        )
                        for slot in _parse_calendar_html(
                            soup,
                            fallback_date=current_date,
                            timezone=timezone,
                            blocked_slots=blocked,
                            facility=facility,
                            sport=sport,
                        ):
                            key = (slot.calendar_id, slot.court_id, slot.start)
                            if key in seen:
                                continue
                            seen.add(key)
                            slots.append(slot)
                        success = True
                    except cloudscraper.exceptions.CloudflareChallengeError as e:
                        error_msg = f"Cloudflare challenge failed for {facility.label} ({facility.id}) on {current_date}: {e}"
                        if retry_count == 0:
                            logger.warning(error_msg)
                            errors.append(error_msg)
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.warning(f"Max retries reached for {facility.label}")
                    except requests.exceptions.HTTPError as e:
                        if e.response and e.response.status_code == 403:
                            error_msg = f"403 Forbidden for {facility.label} ({facility.id}) on {current_date}"
                            if retry_count == 0:
                                logger.warning(error_msg)
                                errors.append(error_msg)
                            retry_count += 1
                            if retry_count > max_retries:
                                logger.warning(f"Max retries reached for {facility.label} (403 Forbidden)")
                        else:
                            # Other HTTP errors, don't retry
                            error_msg = f"HTTP error for {facility.label} ({facility.id}) on {current_date}: {e}"
                            logger.warning(error_msg)
                            errors.append(error_msg)
                            break
                    except Exception as e:
                        error_msg = f"Error fetching {facility.label} ({facility.id}) on {current_date}: {type(e).__name__}: {e}"
                        if retry_count == 0:
                            logger.warning(error_msg, exc_info=True)
                            errors.append(error_msg)
                        retry_count += 1
                        if retry_count > max_retries:
                            break
                
                # Small delay between requests to avoid rate limiting
                time.sleep(0.5)

    if errors and not slots:
        # If we got errors and no slots, log a summary
        logger.warning(f"Eversport fetch completed with {len(errors)} errors and {len(slots)} slots found")
    elif errors:
        logger.info(f"Eversport fetch completed with {len(errors)} errors but {len(slots)} slots found")

    return slots


def _fetch_calendar_html(
    scraper: cloudscraper.CloudScraper,
    *,
    facility: FacilityConfig,
    sport: SportMeta,
    target_date: date,
    timeout: int,
) -> str:
    data = {
        "facilityId": facility.id,
        "facilitySlug": facility.slug,
        "sport[id]": sport.id,
        "sport[slug]": sport.slug,
        "sport[name]": sport.name,
        "sport[uuid]": sport.uuid,
        "date": target_date.isoformat(),
        "type": "user",
    }
    # Update headers for POST request to match form submission
    # Merge with existing headers rather than replacing them
    post_headers = dict(scraper.headers)
    post_headers.update({
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://www.eversports.at',
        'Referer': facility.booking_url,
        'X-Requested-With': 'XMLHttpRequest',
    })
    response = scraper.post(
        CALENDAR_ENDPOINT,
        data=data,
        timeout=timeout,
        headers=post_headers,
    )
    response.raise_for_status()
    return response.text


def _parse_calendar_html(
    soup: BeautifulSoup | str,
    fallback_date: date,
    timezone: ZoneInfo,
    blocked_slots: set[tuple[str, int, str]] | None = None,
    *,
    facility: FacilityConfig,
    sport: SportMeta,
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
            calendar_label = (
                court_row.get("data-area")
                or sport.name
                or facility.label
            )

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
                    facility=facility,
                    sport=sport,
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
    calendar_label: str | None,
    facility: FacilityConfig,
    sport: SportMeta,
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
        calendar_id=facility.id,
        calendar_label=calendar_label or sport.name or facility.label,
        court_id=court_id,
        court_label=court_label,
        start=start_dt,
        end=end_dt,
        duration_minutes=duration_minutes,
        price_eur=price_value,
        price_code=price_code,
        source_url=facility.booking_url,
        provider=PROVIDER,
        sport="tennis",
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
    *,
    facility: FacilityConfig,
    start_date: date,
    court_ids: set[str],
    timeout: int,
) -> set[tuple[str, int, str]]:
    if not court_ids:
        return set()

    params: list[tuple[str, str]] = [
        ("facilityId", facility.id),
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
    slot_entries = []
    if isinstance(payload, dict):
        slots_value = payload.get("slots", [])
        if isinstance(slots_value, list):
            slot_entries = slots_value
    for entry in slot_entries:
        if not isinstance(entry, dict):
            continue
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
