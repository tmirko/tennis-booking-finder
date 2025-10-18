from __future__ import annotations

import csv
import io
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
import streamlit as st
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.append(str(Path(__file__).resolve().parent.joinpath("src")))

from tennis_booking_finder.cli import (
    DEFAULT_TIMEZONE,
    DEFAULT_TIMEOUT,
    USER_AGENT,
    iter_pages,
)

st.set_page_config(page_title="Tennis Booking Finder", layout="wide")


@st.cache_data(ttl=600)
def load_slots(
    pages: int,
    timezone_name: str,
    timeout: int,
    filter_date_str: str | None,
) -> dict[str, Any]:
    """Fetch slots and prepare structured rows for display."""

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    tz = ZoneInfo(timezone_name)
    slots = list(
        iter_pages(
            session=session,
            pages=pages,
            timezone=tz,
            timeout=timeout,
        )
    )

    if filter_date_str:
        target_date = datetime.strptime(filter_date_str, "%Y-%m-%d").date()
        slots = [slot for slot in slots if slot.start.date() == target_date]

    slots.sort(key=lambda slot: (slot.start, slot.court_label, slot.calendar_id, slot.court_id))

    rows: list[dict[str, Any]] = []
    for slot in slots:
        rows.append(
            {
                "source_url": slot.source_url,
                "calendar_label": slot.calendar_label,
                "court_label": slot.court_label,
                "day": slot.start.strftime("%Y-%m-%d"),
                "start": slot.start.strftime("%H:%M"),
                "duration_minutes": slot.duration_minutes,
                "price": slot.price_eur,
            }
        )

    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "rows": rows,
        "generated_at": generated_at,
        "timezone": timezone_name,
    }


def build_csv(rows: list[dict[str, Any]]) -> str:
    """Return CSV representation for download."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    headers = [
        "source_url",
        "calendar_label",
        "court_label",
        "day",
        "start",
        "duration_minutes",
        "price",
    ]
    writer.writerow(headers)
    for row in rows:
        price_value = row["price"]
        price_text = f"{price_value:.2f}" if isinstance(price_value, (int, float)) else ""
        writer.writerow(
            [
                row["source_url"],
                row["calendar_label"],
                row["court_label"],
                row["day"],
                row["start"],
                row["duration_minutes"],
                price_text,
            ]
        )
    return buffer.getvalue()


def main() -> None:
    st.title("Tennis Booking Finder")
    st.caption("Live availability fetched directly from LTM Tennis.")

    with st.sidebar:
        st.header("Options")
        pages = st.slider("Pages to fetch", min_value=1, max_value=6, value=1)
        timezone_input = st.text_input("Timezone", value=DEFAULT_TIMEZONE)
        timeout = st.slider("HTTP timeout (seconds)", min_value=5, max_value=60, value=DEFAULT_TIMEOUT)
        filter_date_input = st.date_input(
            "Filter by date",
            value=None,
            help="Leave empty to see all upcoming slots.",
        )
        refresh = st.button("Refresh data", type="primary")

    if refresh:
        load_slots.clear()
        st.toast("Cache cleared. Updating…", icon="🔄")

    timezone_name = timezone_input.strip() or DEFAULT_TIMEZONE
    filter_date_str = (
        filter_date_input.isoformat()
        if isinstance(filter_date_input, date)
        else None
    )

    try:
        data = load_slots(pages, timezone_name, timeout, filter_date_str)
    except ZoneInfoNotFoundError:
        st.error(f"Unknown timezone: {timezone_name}")
        return
    except requests.RequestException as exc:
        st.error(f"Failed to fetch reservation data: {exc}")
        return

    rows = data["rows"]
    st.subheader("Available Slots")
    st.caption(
        f"Last updated {data['generated_at']} (cache refreshes every 10 minutes)."
    )

    col_metrics = st.columns(2)
    col_metrics[0].metric("Slots found", len(rows))
    col_metrics[1].metric("Pages fetched", pages)

    if rows:
        st.dataframe(rows, use_container_width=True)
        csv_payload = build_csv(rows)
        st.download_button(
            "Download CSV",
            csv_payload,
            "tennis_slots.csv",
            "text/csv",
        )
    else:
        st.info("No available slots found for the selected filters.")


if __name__ == "__main__":
    main()
