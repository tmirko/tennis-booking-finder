import sys
import zoneinfo
sys.path.append("src")

from zoneinfo import ZoneInfo
from tennis_booking_finder.sources.eversports import fetch_slots
from datetime import datetime

slots = fetch_slots(timezone=ZoneInfo("Europe/Vienna"), timeout=10)
print([s for s in slots if s.start == datetime(2025, 10, 19, 21, 0, tzinfo=zoneinfo.ZoneInfo(key='Europe/Vienna'))])