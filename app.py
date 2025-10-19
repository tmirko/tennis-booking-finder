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

from tennis_booking_finder.settings import DEFAULT_TIMEOUT, DEFAULT_TIMEZONE, USER_AGENT
from tennis_booking_finder.sources import collect_slots

EVERSPORTS_LOCATIONS: dict[str, str] = {
    "12886": "sporthotel",
    "80214": "ksv",
}


st.set_page_config(page_title="Tennis Booking Finder", layout="wide")


def determine_pages(filter_dates: tuple[str, ...] | None, tz: ZoneInfo) -> int:
    """Return number of 4-day pages to fetch based on selected dates."""

    if not filter_dates:
        return 1

    today = datetime.now(tz).date()
    diffs: list[int] = []
    for date_str in filter_dates:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = (target - today).days
        if diff >= 0:
            diffs.append(diff)

    if not diffs:
        return 1

    max_diff = max(diffs)
    return max(1, (max_diff // 4) + 1)


@st.cache_data(ttl=600)
def load_slots(
    timezone_name: str,
    timeout: int,
    filter_dates: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Fetch slots and prepare structured rows for display."""

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    tz = ZoneInfo(timezone_name)
    pages_to_fetch = determine_pages(filter_dates, tz)
    target_dates: list[date] | None = None
    if filter_dates:
        target_dates = []
        for date_str in filter_dates:
            try:
                target_dates.append(datetime.strptime(date_str, "%Y-%m-%d").date())
            except ValueError:
                continue

    slots = collect_slots(
        session=session,
        pages=pages_to_fetch,
        timezone=tz,
        timeout=timeout,
        dates=target_dates,
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

        source_url = slot.source_url
        if slot.provider == "ltm":
            facility_type = "air dome" if "c=662" in source_url else "indoor"
            location = "ltm"
        elif slot.provider == "eversports":
            location = EVERSPORTS_LOCATIONS.get(slot.calendar_id, "eversports")
            if location == "ksv" and slot.court_label.lower().startswith("hallenplatz"):
                facility_type = "indoor"
            elif location == "ksv":
                facility_type = "outdoor"
            else:
                facility_type = "indoor"
        else:
            facility_type = "indoor"
            location = slot.provider or "unknown"

        if slot.calendar_label == "Reservierung Festhalle":
            if slot.court_label.strip() == "Platz 5 Hartplatz":
                surface = "hard"
            else:
                surface = "carpet"
        elif slot.calendar_label == "Reservierung Traglufthalle":
            surface = "clay"
        else:
            surface = slot.calendar_label

        court_label_normalized = slot.court_label.lower()
        if "teppichgranulat" in court_label_normalized:
            surface = "carpet"
        elif "opticourt" in court_label_normalized:
            surface = "hard"
        elif location == "ksv":
            surface = "clay"

        rows.append(
            {
                "slot": f"{day_text} {start_text}",
                "minutes": slot.duration_minutes,
                "surface": surface,
                "type": facility_type,
                "price": slot.price_eur,
                "court": slot.court_label,
                "location": location,
                "url": source_url,
            }
        )

    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "rows": rows,
        "generated_at": generated_at,
        "timezone": timezone_name,
        "pages": pages_to_fetch,
    }


def main() -> None:
    st.title("Vienna Court Finder")
    st.caption("Live availability fetched directly from court providers (LTM Tennis).")

    with st.sidebar:
        st.header("Options")
        st.caption(f"Times displayed in {DEFAULT_TIMEZONE}.")
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

    timezone_name = DEFAULT_TIMEZONE
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
        data = load_slots(timezone_name, timeout, filter_dates)
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

    st.metric("Slots found", len(rows))

    if rows:
        st.dataframe(
            rows,
            use_container_width=True,
            column_config={
                "minutes": st.column_config.NumberColumn("minutes", format="%d"),
                "surface": st.column_config.TextColumn("surface"),
                "price": st.column_config.NumberColumn("price", format="€%.2f"),
                "url": st.column_config.LinkColumn("url", display_text="link"),
                "location": st.column_config.TextColumn("location"),
            },
        )
    else:
        st.info("No available slots found for the selected filters.")


if __name__ == "__main__":
    main()
