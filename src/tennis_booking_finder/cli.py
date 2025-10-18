from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional, Sequence
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_URL = "https://ltm.tennisplatz.info/reservierung"
SEED_URLS = [
    BASE_URL,
    f"{BASE_URL}?c=662",
]
DEFAULT_TIMEZONE = "Europe/Vienna"
DEFAULT_TIMEOUT = 30
USER_AGENT = "tennis-booking-finder/0.1"


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

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation of the slot."""

        return {
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


def fetch_html(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, object]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Fetch the HTML page and return its text along with the resolved URL."""

    logging.debug("Fetching %s params=%s", url, params)
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.text, response.url


def parse_price_map(soup: BeautifulSoup) -> dict[str, float]:
    """Extract a mapping of CSS price classes to numeric euro values."""

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


PRICE_COLOR_PATTERN = re.compile(
    r"\.price(?P<code>\d+):after[^}]*background:\s*(?P<color>#[0-9a-fA-F]{3,6})",
    re.IGNORECASE | re.DOTALL,
)


def parse_price_colors(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the colour used for each price class from embedded styles."""

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
    """Yield every available slot contained in a calendar component."""

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
    """Parse all available slots for a single day block."""

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
    """Convert a slot HTML tag into a Slot model if it is available."""

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
    )


def extract_next_href(soup: BeautifulSoup) -> Optional[str]:
    """Return the relative URL of the next page if available."""

    nav = soup.select_one(".calendar-head .time-nav-right")
    if not nav:
        return None
    href = nav.get("data-href")
    if href and href.strip() and href != "#":
        return href.strip()
    return None


def iter_pages(
    *,
    session: requests.Session,
    pages: int,
    timezone: ZoneInfo,
    timeout: int,
) -> Iterable[Slot]:
    """Iterate through calendar pages for each configured seed URL."""

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

            try:
                html, resolved = fetch_html(session, url, params=params, timeout=timeout)
            except requests.RequestException as exc:  # pragma: no cover - surfaced to CLI
                logging.error("Failed to fetch %s: %s", url, exc)
                raise

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


def format_slots_text(slots: Sequence[Slot]) -> str:
    """Render the collected slots as a human-readable schedule."""

    if not slots:
        return "No available slots found."

    lines: list[str] = []
    current_key: Optional[str] = None
    for slot in slots:
        date_key = slot.start.strftime("%Y-%m-%d (%a)")
        if date_key != current_key:
            if current_key is not None:
                lines.append("")
            lines.append(date_key)
            current_key = date_key
        time_window = f"{slot.start:%H:%M}-{slot.end:%H:%M}"
        price_text = (
            f"€{slot.price_eur:.2f}"
            if slot.price_eur is not None
            else "n/a"
        )
        if slot.price_code and slot.price_eur is not None:
            price_text = f"{price_text} ({slot.price_code})"
        lines.append(
            "  "
            + " | ".join(
                [
                    time_window,
                    f"{slot.court_label} [{slot.court_id}]",
                    f"{slot.calendar_label} [{slot.calendar_id}]",
                    f"price {price_text}",
                ]
            )
        )
    return "\n".join(lines)


def format_slots_structured(slots: Sequence[Slot]) -> str:
    """Render slots as a fixed-width table of key attributes."""

    if not slots:
        return "No available slots found."

    headers = (
        "source_url",
        "calendar_label",
        "court_label",
        "day",
        "start",
        "duration_minutes",
        "price",
    )

    rows: list[tuple[str, ...]] = []
    for slot in slots:
        day = slot.start.strftime("%Y-%m-%d")
        start = slot.start.strftime("%H:%M")
        price = (
            f"{slot.price_eur:.2f}"
            if slot.price_eur is not None
            else "n/a"
        )
        rows.append(
            (
                slot.source_url,
                slot.calendar_label,
                slot.court_label,
                day,
                start,
                str(slot.duration_minutes),
                price,
            )
        )

    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row)]

    def render_row(values: Sequence[str]) -> str:
        return " | ".join(value.ljust(width) for value, width in zip(values, widths))

    separator = "-+-".join("-" * width for width in widths)

    lines = [render_row(headers), separator]
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Construct the CLI argument parser and parse inputs."""

    parser = argparse.ArgumentParser(
        description="Scrape available tennis reservation slots from LTM Tennis.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of 4-day calendar pages to crawl by following the next navigation (default: 1).",
    )
    parser.add_argument(
        "--timezone",
        "-t",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used to display times (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--filter-date",
        help="Restrict results to a specific date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=("text", "json", "structured"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging for troubleshooting.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the CLI application."""

    args = parse_args(argv)

    if args.pages < 1:
        print("--pages must be at least 1", file=sys.stderr)
        return 2

    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(level=log_level, format="[%(levelname)s] %(message)s")

    try:
        timezone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        print(f"Unknown timezone: {args.timezone}", file=sys.stderr)
        return 2

    filter_date = None
    if args.filter_date:
        try:
            filter_date = datetime.strptime(args.filter_date, "%Y-%m-%d").date()
        except ValueError:
            print("--filter-date must follow YYYY-MM-DD", file=sys.stderr)
            return 2

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        slots = list(
            iter_pages(
                session=session,
                pages=args.pages,
                timezone=timezone,
                timeout=args.timeout,
            )
        )
    except requests.RequestException as exc:
        print(f"Failed to fetch reservation data: {exc}", file=sys.stderr)
        return 1

    if filter_date is not None:
        slots = [slot for slot in slots if slot.start.date() == filter_date]

    slots.sort(key=lambda slot: (slot.start, slot.court_label, slot.calendar_id, slot.court_id))

    if args.format == "json":
        print(json.dumps([slot.to_dict() for slot in slots], indent=2))
    elif args.format == "structured":
        print(format_slots_structured(slots))
    else:
        print(format_slots_text(slots))

    return 0


if __name__ == "__main__":
    sys.exit(main())
