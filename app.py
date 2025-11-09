from __future__ import annotations

import csv
import io
import logging
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

# Configure logging to show warnings and errors
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)

EVERSPORTS_LOCATIONS: dict[str, str] = {
    "12886": "sporthotel",
    "80214": "ksv",
    "12782": "tennis point",
}


st.set_page_config(page_title="Court Booking Finder", layout="wide")


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
    sport: str,
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
        sport=sport,
    )

    if filter_dates:
        target_dates = set(filter_dates)
        slots = [
            slot
            for slot in slots
            if slot.start.strftime("%Y-%m-%d") in target_dates
        ]

    slots.sort(key=lambda slot: (slot.start, slot.court_label, slot.calendar_id, slot.court_id))

    # Track which providers returned slots
    providers_found = {slot.provider for slot in slots if slot.provider}
    
    rows: list[dict[str, Any]] = []
    for slot in slots:
        start_label = slot.start.strftime("%Y-%m-%d %H:%M")
        if slot.start.date() == slot.end.date():
            slot_label = f"{start_label}-{slot.end.strftime('%H:%M')}"
        else:
            slot_label = f"{start_label} - {slot.end.strftime('%Y-%m-%d %H:%M')}"

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
            elif location == "tennis point":
                facility_type = "indoor"
            else:
                facility_type = "indoor"
        elif slot.provider == "padeldome":
            # Extract location from calendar_label (e.g., "Reservierung Padel ERDBERG" -> "padeldome erdberg")
            calendar_lower = slot.calendar_label.lower()
            facility_type = "indoor"  # Default for padel
            if "erdberg" in calendar_lower:
                location = "padeldome erdberg"
            elif "alt erlaa" in calendar_lower or "alterlaa" in calendar_lower:
                location = "padeldome alt erlaa"
            elif "alte donau" in calendar_lower:
                location = "padeldome alte donau"
                if "outdoor" in calendar_lower:
                    facility_type = "outdoor"
                else:
                    facility_type = "indoor"
            else:
                location = "padeldome"
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
        if slot.sport == "padel":
            surface = "carpet"
        elif "teppichgranulat" in court_label_normalized:
            surface = "carpet"
        elif "opticourt" in court_label_normalized:
            surface = "hard"
        elif location == "ksv":
            surface = "clay"
        elif location == "tennis point":
            surface = "carpet"

        rows.append(
            {
                "slot": slot_label,
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
        "providers_found": providers_found,
    }


def _sanitize_option(option: str) -> str:
    return option.lower().replace(" ", "_")


def render_checkbox_filter(
    container,
    label: str,
    options: list[str],
    key_prefix: str,
    default_selected: set[str] | None = None,
) -> list[str]:
    """Render a checkbox group and return the selected entries."""

    if not options:
        return []

    container.markdown(f"**{label}**")
    selected: list[str] = []
    for option in options:
        sanitized_key = _sanitize_option(option)
        checkbox_key = f"{key_prefix}_{sanitized_key}"
        is_checked = option in default_selected if default_selected is not None else True
        state_key = checkbox_key
        if state_key not in st.session_state:
            st.session_state[state_key] = is_checked
        if container.checkbox(option, key=checkbox_key):
            selected.append(option)
    return selected


def main() -> None:
    st.title("Vienna Court Finder")
    st.caption("Live availability fetched directly from court providers.")

    with st.sidebar:
        # st.header("Options")
        # st.caption(f"Times displayed in {DEFAULT_TIMEZONE}.")
        sport = st.selectbox(
            "Sport",
            options=["tennis", "padel"],
            index=0,  # Tennis is default
            help="Select the sport to search for available courts.",
        )
        filter_date_input = st.date_input(
            "Filter by date",
            value=date.today(),
            help="Pick a single day to filter upcoming slots.",
        )
        refresh = st.button("Refresh data", type="primary")
        filters_placeholder = st.container()
        timeout = st.slider("HTTP timeout (seconds)", min_value=5, max_value=60, value=DEFAULT_TIMEOUT)


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
        data = load_slots(timezone_name, timeout, filter_dates, sport)
    except ZoneInfoNotFoundError:
        st.error(f"Unknown timezone: {timezone_name}")
        return
    except requests.RequestException as exc:
        st.error(f"Failed to fetch reservation data: {exc}")
        return

    rows = data["rows"]
    providers_found = data.get("providers_found", set())
    
    # Show warning if Eversport is expected but not found (for tennis sport)
    if sport == "tennis" and "eversports" not in providers_found:
        st.warning(
            "⚠️ Eversport courts are not currently available. This may be due to Cloudflare protection "
            "blocking requests from the server. Check the server logs for details. Other providers are working normally."
        )
    
    surface_options = sorted({row["surface"] for row in rows})
    type_options = sorted({row["type"] for row in rows})
    location_options = sorted({row["location"] for row in rows})

    prev_selected_surfaces = st.session_state.get("selected_surfaces", [])
    prev_selected_types = st.session_state.get("selected_types", [])
    prev_selected_locations = st.session_state.get("selected_locations", [])

    filter_signature = (
        sport,
        tuple(surface_options),
        tuple(type_options),
        tuple(location_options),
        filter_dates,
    )
    previous_signature = st.session_state.get("filter_signature")
    if previous_signature != filter_signature:
        def refresh_checkbox_state(prefix: str, options: list[str], previous: list[str], exclude_defaults: set[str] | None = None, include_defaults: set[str] | None = None) -> list[str]:
            for key in [k for k in st.session_state.keys() if k.startswith(prefix)]:
                st.session_state.pop(key)
            if not options:
                return []

            available = set(options)
            retained = available & set(previous)
            if not retained:
                retained = available
                if exclude_defaults:
                    retained = retained - exclude_defaults
            
            # Always include specified defaults that are available
            if include_defaults:
                retained = retained | (include_defaults & available)

            for option in options:
                checkbox_key = f"{prefix}{_sanitize_option(option)}"
                st.session_state[checkbox_key] = option in retained

            return sorted(retained)

        # Set default selections for padel
        exclude_locations = set()
        include_locations = set()
        exclude_types = set()
        include_types = set()
        if sport == "padel":
            include_locations.add("padeldome erdberg")
            include_types.add("outdoor")

        prev_selected_surfaces = refresh_checkbox_state("surface_filter_", surface_options, prev_selected_surfaces)
        prev_selected_types = refresh_checkbox_state("type_filter_", type_options, prev_selected_types, exclude_defaults=exclude_types, include_defaults=include_types)
        prev_selected_locations = refresh_checkbox_state("location_filter_", location_options, prev_selected_locations, exclude_defaults=exclude_locations, include_defaults=include_locations)
        st.session_state["filter_signature"] = filter_signature
        st.session_state["selected_surfaces"] = prev_selected_surfaces
        st.session_state["selected_types"] = prev_selected_types
        st.session_state["selected_locations"] = prev_selected_locations

    def current_checked(prefix: str, options: list[str]) -> set[str]:
        return {
            option
            for option in options
            if st.session_state.get(f"{prefix}{_sanitize_option(option)}", True)
        }

    default_surface_selection = current_checked("surface_filter_", surface_options)
    default_type_selection = current_checked("type_filter_", type_options)
    default_location_selection = current_checked("location_filter_", location_options)

    selected_surfaces: list[str] = []
    selected_types: list[str] = []
    selected_locations: list[str] = []

    with filters_placeholder:
        # st.subheader("Filters")
        if rows:
            # st.caption("Uncheck any category to hide matching slots.")
            selected_locations = render_checkbox_filter(
                st,
                "Location",
                location_options,
                "location_filter",
                default_selected=default_location_selection,
            )
            selected_types = render_checkbox_filter(
                st,
                "Type",
                type_options,
                "type_filter",
                default_selected=default_type_selection,
            )
            selected_surfaces = render_checkbox_filter(
                st,
                "Surface",
                surface_options,
                "surface_filter",
                default_selected=default_surface_selection,
            )
        else:
            st.caption("Filters become available once slots load.")

    st.session_state["selected_surfaces"] = selected_surfaces
    st.session_state["selected_types"] = selected_types
    st.session_state["selected_locations"] = selected_locations

    filtered_rows = rows
    if surface_options:
        filtered_rows = [row for row in filtered_rows if row["surface"] in selected_surfaces]
    if type_options:
        filtered_rows = [row for row in filtered_rows if row["type"] in selected_types]
    if location_options:
        filtered_rows = [row for row in filtered_rows if row["location"] in selected_locations]

    st.subheader("Available Slots")
    st.caption(
        f"Last updated {data['generated_at']} (cache refreshes every 10 minutes)."
    )

    st.metric("Slots found", len(filtered_rows))

    if filtered_rows:
        st.dataframe(
            filtered_rows,
            use_container_width=True,
            column_config={
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
