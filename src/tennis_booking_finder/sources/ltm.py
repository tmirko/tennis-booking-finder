from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from zoneinfo import ZoneInfo

from ..models import Slot

BASE_URL = "https://ltm.tennisplatz.info/reservierung"
SEED_URLS = [
    BASE_URL,
    f"{BASE_URL}?c=662",
]
PROVIDER = "ltm"

PRICE_COLOR_PATTERN = re.compile(
    r"\.price(?P<code>\d+):after[^}]*background:\s*(?P<color>#[0-9a-fA-F]{3,6})",
    re.IGNORECASE | re.DOTALL,
)


def fetch_html(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, object]] = None,
    timeout: int,
) -> tuple[str, str]:
    logging.debug("Fetching %s params=%s", url, params)
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.text, response.url


def parse_price_map(soup: BeautifulSoup) -> dict[str, float]:
    prices: dict[str, float] = {}
    for price_element in soup.select("div.pricebox div.price"):
        classes = price_element.get("class", [])
        code = next((c for c in classes if c.startswith("price") and c != "price"), None)
        if not code:
            continue
        text = price_element.get_text(strip=True).replace("\xa0", " ")
        value_text = text.replace("€", "").strip().replace(",", ".")
        try:
            prices[code] = float(value_text)
        except ValueError:
            logging.debug("Skipping malformed price value: %s", text)
    logging.debug("Parsed price codes: %s", prices)
    return prices


def parse_price_colors(soup: BeautifulSoup) -> dict[str, str]:
    css_chunks = [style.get_text("\n", strip=False) for style in soup.find_all("style")]
    if not css_chunks:
        return {}

    css_text = "\n".join(css_chunks)
    colors: dict[str, str] = {}
    for match in PRICE_COLOR_PATTERN.finditer(css_text):
        code = f"price{match.group('code')}"
        color = match.group("color").lower()
        colors[code] = color
    if colors:
        logging.debug("Parsed price colours: %s", colors)
    return colors


def parse_available_slots(
    calendar: Tag,
    *,
    price_map: dict[str, float],
    timezone: ZoneInfo,
    calendar_label: str,
    source_url: str,
) -> Iterator[Slot]:
    calendar_id = calendar.get("data-cid", "unknown")
    head_days = calendar.select(".calendar-head div.day")
    body_days = calendar.select(".cs-area div.day")

    if not head_days or not body_days:
        logging.debug("Calendar %s has no day entries", calendar_id)
        return

    if len(head_days) != len(body_days):
        logging.warning(
            "Day header/body mismatch for calendar %s: %s vs %s",
            calendar_id,
            len(head_days),
            len(body_days),
        )

    for head_tag, body_tag in zip(head_days, body_days):
        yield from _parse_day_slots(
            head_tag,
            body_tag,
            price_map=price_map,
            timezone=timezone,
            calendar_id=calendar_id,
            calendar_label=calendar_label,
            source_url=source_url,
        )


def _parse_day_slots(
    head_tag: Tag,
    body_tag: Tag,
    *,
    price_map: dict[str, float],
    timezone: ZoneInfo,
    calendar_id: str,
    calendar_label: str,
    source_url: str,
) -> Iterator[Slot]:
    timestamp_raw = head_tag.get("data-dt")
    day_start: Optional[datetime] = None
    if timestamp_raw and timestamp_raw.isdigit():
        day_start = datetime.fromtimestamp(int(timestamp_raw), timezone)
        logging.debug("Day start inferred from timestamp %s", day_start)

    courts_header = body_tag.select_one(".day-head .day-courts")
    if not courts_header:
        logging.debug("Missing court header in calendar %s", calendar_id)
        return

    court_labels = [
        court.get_text(strip=True)
        for court in courts_header.select(".court")
    ]
    court_columns = body_tag.select(".day-body div.court[data-cid]")

    if len(court_labels) != len(court_columns):
        logging.warning(
            "Court header/body mismatch for calendar %s on %s: %s vs %s",
            calendar_id,
            day_start.date() if day_start else "unknown date",
            len(court_labels),
            len(court_columns),
        )

    for label, court_column in zip(court_labels, court_columns):
        court_id = court_column.get("data-cid", "")
        for slot_tag in court_column.select("div.slot"):
            slot = _build_slot(
                slot_tag,
                court_id=court_id,
                court_label=label,
                calendar_id=calendar_id,
                calendar_label=calendar_label,
                timezone=timezone,
                day_start=day_start,
                price_map=price_map,
                source_url=source_url,
            )
            if slot:
                yield slot


def _build_slot(
    slot_tag: Tag,
    *,
    court_id: str,
    court_label: str,
    calendar_id: str,
    calendar_label: str,
    timezone: ZoneInfo,
    day_start: Optional[datetime],
    price_map: dict[str, float],
    source_url: str,
) -> Optional[Slot]:
    classes = [cls for cls in slot_tag.get("class", []) if cls != "slot"]
    if "av" not in classes:
        return None

    start_raw = slot_tag.get("data-begin")
    span_raw = slot_tag.get("data-size") or "1"
    if not start_raw:
        logging.debug("Skipping slot without start timestamp")
        return None

    try:
        start_dt = datetime.fromtimestamp(int(start_raw), timezone)
    except (TypeError, ValueError):
        logging.debug("Invalid data-begin value: %s", start_raw)
        return None

    try:
        duration_blocks = max(int(span_raw), 1)
    except (TypeError, ValueError):
        logging.debug("Invalid data-size value: %s", span_raw)
        duration_blocks = 1

    end_dt = start_dt + timedelta(hours=duration_blocks)

    price_code = next((cls for cls in classes if cls.startswith("price") and cls != "price"), None)
    price_value = price_map.get(price_code)

    return Slot(
        calendar_id=calendar_id,
        calendar_label=calendar_label,
        court_id=court_id,
        court_label=court_label,
        start=start_dt,
        end=end_dt,
        duration_minutes=duration_blocks * 60,
        price_eur=price_value,
        price_code=price_code,
        source_url=source_url,
        provider=PROVIDER,
    )


def extract_next_href(soup: BeautifulSoup) -> Optional[str]:
    nav = soup.select_one(".calendar-head .time-nav-right")
    if not nav:
        return None
    href = nav.get("data-href")
    if href and href.strip() and href != "#":
        return href.strip()
    return None


def fetch_slots(
    *,
    session: requests.Session,
    pages: int,
    timezone: ZoneInfo,
    timeout: int,
) -> Iterable[Slot]:
    seen_urls: set[str] = set()
    price_lookup: dict[str, float] = {}
    color_price_by_seed: dict[str, dict[str, float]] = {}

    for seed in SEED_URLS:
        url = seed
        params = None

        for page_index in range(pages):
            if url in seen_urls:
                logging.debug("Skipping already visited URL %s", url)
                break

            html, resolved = fetch_html(session, url, params=params, timeout=timeout)
            seen_urls.add(resolved)

            soup = BeautifulSoup(html, "html.parser")
            page_title = soup.select_one("h1")
            calendar_label = page_title.get_text(" ", strip=True) if page_title else "Tennis Booking"
            page_colors = parse_price_colors(soup)
            price_map = parse_price_map(soup)
            if price_map:
                price_lookup.update(price_map)

            color_price_map = color_price_by_seed.setdefault(seed, {})
            if page_colors:
                page_color_price: dict[str, float] = {}
                for code, price_value in price_map.items():
                    color = page_colors.get(code)
                    if color and color not in page_color_price:
                        page_color_price[color] = price_value
                color_price_map.update(page_color_price)

                for code, color in page_colors.items():
                    if code in price_lookup:
                        continue
                    if color in page_color_price:
                        price_lookup[code] = page_color_price[color]
                    elif color in color_price_map:
                        price_lookup[code] = color_price_map[color]

            for calendar in soup.select("div.calendar"):
                yield from parse_available_slots(
                    calendar,
                    price_map=price_lookup,
                    timezone=timezone,
                    calendar_label=calendar_label,
                    source_url=resolved,
                )

            next_href = extract_next_href(soup)
            logging.debug("Next href discovered: %s", next_href)
            if not next_href:
                break

            absolute_next = urljoin(BASE_URL, next_href)
            if absolute_next in seen_urls:
                logging.debug("Already visited %s, stopping traversal", absolute_next)
                break

            url = absolute_next
            params = None
            logging.debug("Moving to next page %s (%d/%d) for seed %s", url, page_index + 1, pages, seed)
