from __future__ import annotations

import csv
import io
import sys
from datetime import date, datetime, timedelta
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
    filter_dates: tuple[str, ...] | None,
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

    if filter_dates:
        target_dates = set(filter_dates)
        slots = [
            slot
            for slot in slots
            if slot.start.strftime("%Y-%m-%d") in target_dates
        ]

    slots.sort(key=lambda slot: (slot.start, slot.court_label, slot.calendar_id, slot.court_id))

    rows: list[dict[str, Any]] = []
    for slot in slots:
        day_text = slot.start.strftime("%Y-%m-%d")
        start_text = slot.start.strftime("%H:%M")

        if slot.calendar_label == "Reservierung Festhalle":
            if slot.court_label.strip() == "Platz 5 Hartplatz":
                surface = "hard"
            else:
                surface = "carpet"
        elif slot.calendar_label == "Reservierung Traglufthalle":
            surface = "clay"
        else:
            surface = slot.calendar_label

        source_url = slot.source_url
        facility_type = "air dome" if "c=662" in source_url else "indoor"

        rows.append(
            {
                "slot": f"{day_text} {start_text}",
                "minutes": slot.duration_minutes,
                "surface": surface,
                "type": facility_type,
                "price": slot.price_eur,
                "court": slot.court_label,
                "url": source_url,
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
        "slot",
        "minutes",
        "surface",
        "type",
        "price",
        "court",
        "url",
    ]
    writer.writerow(headers)
    for row in rows:
        price_value = row["price"]
        price_text = (
            f"{price_value:.2f}" if isinstance(price_value, (int, float)) else ""
        )
        writer.writerow(
            [
                row["slot"],
                row["minutes"],
                row["surface"],
                row["type"],
                price_text,
                row["court"],
                row["url"],
            ]
        )
    return buffer.getvalue()


def main() -> None:
    st.title("Vienna Court Finder")
    st.caption("Live availability fetched directly from court providers (LTM Tennis).")

    with st.sidebar:
        st.header("Options")
        pages = st.slider("Pages to fetch", min_value=1, max_value=6, value=1)
        timezone_input = st.text_input("Timezone", value=DEFAULT_TIMEZONE)
        timeout = st.slider("HTTP timeout (seconds)", min_value=5, max_value=60, value=DEFAULT_TIMEOUT)
        filter_date_input = st.date_input(
            "Filter by date",
            value=(),
            help="Pick a single day or drag to select a range. Leave empty for all upcoming slots.",
        )
        refresh = st.button("Refresh data", type="primary")

    if refresh:
        load_slots.clear()
        st.toast("Cache cleared. Updating…", icon="🔄")

    timezone_name = timezone_input.strip() or DEFAULT_TIMEZONE
    filter_dates: tuple[str, ...] | None = None
    if isinstance(filter_date_input, date):
        filter_dates = (filter_date_input.isoformat(),)
    elif isinstance(filter_date_input, tuple):
        selected_dates = [d for d in filter_date_input if isinstance(d, date)]
        if len(selected_dates) == 2:
            start_date, end_date = sorted(selected_dates)
            total_days = (end_date - start_date).days
            filter_dates = tuple(
                (start_date + timedelta(days=offset)).isoformat()
                for offset in range(total_days + 1)
            )
        elif len(selected_dates) == 1:
            filter_dates = (selected_dates[0].isoformat(),)
        elif len(selected_dates) > 2:
            filter_dates = tuple(date_value.isoformat() for date_value in selected_dates)

    try:
        data = load_slots(pages, timezone_name, timeout, filter_dates)
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
        st.dataframe(
            rows,
            use_container_width=True,
            column_config={
                "minutes": st.column_config.NumberColumn("minutes", format="%d"),
                "surface": st.column_config.TextColumn("surface"),
                "price": st.column_config.NumberColumn("price", format="€%.2f"),
                "url": st.column_config.LinkColumn("url"),
            },
        )
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
