from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import Optional, Sequence

import requests
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Slot
from .settings import DEFAULT_TIMEOUT, DEFAULT_TIMEZONE, USER_AGENT
from .sources import collect_slots


def format_slots_text(slots: Sequence[Slot]) -> str:
    """Render slots grouped by day using a human-readable text layout."""

    if not slots:
        return "No available slots found."

    lines: list[str] = []
    current_key: Optional[str] = None
    for slot in slots:
        date_key = slot.start.strftime("%A %Y-%m-%d")
        if date_key != current_key:
            if current_key is not None:
                lines.append("")
            lines.append(date_key)
            current_key = date_key

        time_window = f"{slot.start:%H:%M}-{slot.end:%H:%M}"
        price_text = "n/a"
        if slot.price_eur is not None:
            price_text = f"€{slot.price_eur:.2f}"
            if slot.price_code:
                price_text = f"{price_text} ({slot.price_code})"
        elif slot.price_code:
            price_text = slot.price_code

        parts = [
            time_window,
            f"{slot.court_label} [{slot.court_id}]",
            f"{slot.calendar_label} [{slot.calendar_id}]",
            f"price {price_text}",
        ]
        if slot.provider:
            parts.append(f"provider {slot.provider}")
        lines.append("  " + " | ".join(parts))

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
        slots = collect_slots(
            session=session,
            pages=args.pages,
            timezone=timezone,
            timeout=args.timeout,
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
