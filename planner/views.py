from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view


DutyStatus = Literal["OFF", "SB", "D", "ON"]


def _looks_like_city_state(s: str) -> bool:
    v = (s or "").strip()
    if not v:
        return False
    # Basic "City, ST" or "City, State" format check.
    import re

    return re.match(r"^[^,]+,\s*([A-Za-z]{2}|[A-Za-z][A-Za-z .'-]{2,})$", v) is not None


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    label: str


def _haversine_meters(a: GeoPoint, b: GeoPoint) -> float:
    # Great-circle distance.
    r = 6371000.0
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _parse_start_dt(value: str | None) -> datetime:
    if not value:
        # Default: "now" in UTC; client can provide explicit timezone offset.
        return datetime.now(tz=timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _min_since_midnight(dt: datetime) -> int:
    dt0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((dt - dt0).total_seconds() // 60)


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    # Keep the driver/home-terminal time base (timezone) stable in the API output.
    # The frontend will render these timestamps in the user's local tz.
    return dt.isoformat()


def _short_place(label: str) -> str:
    """
    Reduce verbose geocoder labels to a paper-log friendly "City, ST" (best-effort).
    """
    s = (label or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in s.split(",") if p.strip()]
    us_state_abbrev = {
        "Alabama": "AL",
        "Alaska": "AK",
        "Arizona": "AZ",
        "Arkansas": "AR",
        "California": "CA",
        "Colorado": "CO",
        "Connecticut": "CT",
        "Delaware": "DE",
        "Florida": "FL",
        "Georgia": "GA",
        "Hawaii": "HI",
        "Idaho": "ID",
        "Illinois": "IL",
        "Indiana": "IN",
        "Iowa": "IA",
        "Kansas": "KS",
        "Kentucky": "KY",
        "Louisiana": "LA",
        "Maine": "ME",
        "Maryland": "MD",
        "Massachusetts": "MA",
        "Michigan": "MI",
        "Minnesota": "MN",
        "Mississippi": "MS",
        "Missouri": "MO",
        "Montana": "MT",
        "Nebraska": "NE",
        "Nevada": "NV",
        "New Hampshire": "NH",
        "New Jersey": "NJ",
        "New Mexico": "NM",
        "New York": "NY",
        "North Carolina": "NC",
        "North Dakota": "ND",
        "Ohio": "OH",
        "Oklahoma": "OK",
        "Oregon": "OR",
        "Pennsylvania": "PA",
        "Rhode Island": "RI",
        "South Carolina": "SC",
        "South Dakota": "SD",
        "Tennessee": "TN",
        "Texas": "TX",
        "Utah": "UT",
        "Vermont": "VT",
        "Virginia": "VA",
        "Washington": "WA",
        "West Virginia": "WV",
        "Wisconsin": "WI",
        "Wyoming": "WY",
        "District of Columbia": "DC",
    }

    def abbrev_state(name: str) -> str:
        n = name.strip()
        return us_state_abbrev.get(n, n)

    if len(parts) >= 3 and "county" in parts[1].lower():
        return f"{parts[0]}, {abbrev_state(parts[2])}"
    if len(parts) >= 2:
        return f"{parts[0]}, {abbrev_state(parts[1])}"
    return parts[0]


def _paper_loc(loc: str) -> str:
    s = (loc or "").strip()
    if not s:
        return ""
    if s.lower() == "enroute":
        return "Enroute"
    return _short_place(s) or s


def _round_half_up(x: float) -> int:
    # Match typical UI rounding (0.5 rounds up), avoiding Python's bankers rounding.
    return int(math.floor(x + 0.5))


def _geocode(place: str) -> GeoPoint:
    url = "https://nominatim.openstreetmap.org/search"
    r = requests.get(
        url,
        params={"q": place, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": "hos-logbook-assessment/1.0 (education)"},
        timeout=3,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Could not geocode: {place}")
    item = data[0]
    return GeoPoint(lat=float(item["lat"]), lon=float(item["lon"]), label=item.get("display_name") or place)


def _route(a: GeoPoint, b: GeoPoint) -> dict[str, Any]:
    # Public OSRM. Some networks block this host; in that case we fall back to straight-line.
    url = f"https://router.project-osrm.org/route/v1/driving/{a.lon},{a.lat};{b.lon},{b.lat}"
    r = requests.get(
        url,
        params={
            # Keep response small/fast for long routes.
            "overview": "simplified",
            "geometries": "geojson",
            "steps": "false",
            "annotations": "false",
        },
        timeout=3,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("routes"):
        raise ValueError("No route returned from OSRM.")
    route = data["routes"][0]
    return {
        "distance_m": route["distance"],
        "duration_s": route["duration"],
        "geometry": route["geometry"],
        "legs": route.get("legs", []),
    }


@dataclass
class Segment:
    status: DutyStatus
    start: datetime
    end: datetime
    location: str
    note: str

    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))


def _round_up_to_min(dt: datetime, minutes: int) -> datetime:
    # Round up to next multiple of `minutes`.
    epoch = dt.timestamp()
    step = minutes * 60
    rounded = math.ceil(epoch / step) * step
    return datetime.fromtimestamp(rounded, tz=dt.tzinfo)


def _round_to_min(dt: datetime, minutes: int) -> datetime:
    # Round to nearest multiple of `minutes`.
    epoch = dt.timestamp()
    step = minutes * 60
    rounded = round(epoch / step) * step
    return datetime.fromtimestamp(rounded, tz=dt.tzinfo)


def _plan_timeline(
    *,
    start_dt: datetime,
    legs: list[dict[str, Any]],
    current_cycle_used_h: float,
    current_label: str = "Enroute",
    pickup_label: str = "Pickup",
    dropoff_label: str = "Dropoff",
    pickup_service_h: float = 1.0,
    dropoff_service_h: float = 1.0,
    fuel_every_miles: float = 1000.0,
    fuel_stop_h: float = 0.5,
) -> dict[str, Any]:
    """
    Assumptions (per assessment + prompt):
    - Property-carrying driver.
    - 70hrs/8days cycle. We accept current_cycle_used_h and schedule a 34h restart if we run out.
    - No adverse driving conditions.
    - 30-min break required after 8 cumulative driving hours (break can be OFF/ON/SB; we log it OFF).
    - 11 hours driving max within a 14-hour window, then 10 hours OFF to reset.
    - Fuel at least once every 1,000 miles (modeled as ON duty 30 min stop).
    """

    MAX_DRIVE_MIN = 11 * 60
    MAX_WINDOW_MIN = 14 * 60
    BREAK_AFTER_DRIVE_MIN = 8 * 60
    BREAK_MIN = 30
    OFF_RESET_H = 10.0
    RESTART_H = 34.0
    CYCLE_LIMIT_H = 70.0

    # Driving "rate" for converting route duration ↔ miles segments.
    total_drive_s = sum(leg["duration_s"] for leg in legs)
    total_meters = sum(leg["distance_m"] for leg in legs)
    meters_per_second = (total_meters / total_drive_s) if total_drive_s else 0.0

    segments: list[Segment] = []
    warnings: list[str] = []

    # Quantize to the 15-minute paper-log grid.
    t = _round_to_min(start_dt, 15)
    cycle_used = float(current_cycle_used_h)
    fuel_since_miles = 0.0
    last_location = current_label or "Enroute"

    def add(status: DutyStatus, minutes: int, location: str, note: str) -> None:
        nonlocal t, cycle_used, last_location, drive_since_break_min
        if minutes <= 0:
            return
        start = t
        t = t + timedelta(minutes=minutes)
        segments.append(Segment(status=status, start=start, end=t, location=location, note=note))
        # For paper-log remarks we want "where reported/released" style locations,
        # not "enroute" / transient points. So only advance last_location on non-driving events.
        if location and status != "D":
            last_location = location
        if status in ("D", "ON"):
            cycle_used += minutes / 60.0
        # A consecutive 30-minute non-driving interruption satisfies the break requirement.
        if status in ("OFF", "ON", "SB") and minutes >= BREAK_MIN:
            drive_since_break_min = 0
        # Any qualifying OFF/SB stretch resets "daily" clocks.
        if status in ("OFF", "SB") and minutes >= int(OFF_RESET_H * 60):
            reset_shift()

    def ensure_cycle_ok() -> None:
        nonlocal cycle_used
        if cycle_used < CYCLE_LIMIT_H - 1e-9:
            return
        warnings.append("70-hour/8-day limit reached; scheduling a 34-hour restart.")
        add("SB", int(RESTART_H * 60), location=last_location, note="34-hour restart (cycle reset)")
        cycle_used = 0.0
        reset_shift()

    def cycle_left_minutes() -> int:
        # Only on-duty + driving count against cycle.
        left_h = max(0.0, CYCLE_LIMIT_H - cycle_used)
        return int(math.floor(left_h * 60.0 + 1e-9))

    def consume_cycle_or_restart(minutes_needed: int) -> None:
        """
        Ensure we have enough cycle minutes for an ON/D segment.
        If not, schedule a 34-hour restart BEFORE doing that work.
        """
        if minutes_needed <= 0:
            return
        if minutes_needed <= cycle_left_minutes():
            return
        ensure_cycle_ok()  # if already at/over limit this will restart
        if minutes_needed <= cycle_left_minutes():
            return
        # Not at limit yet, but we can't fit the next work block -> restart now.
        warnings.append("Insufficient cycle hours remaining; scheduling a 34-hour restart before continuing.")
        add("SB", int(RESTART_H * 60), location=last_location, note="34-hour restart (cycle reset)")
        cycle_used = 0.0
        reset_shift()

    # The schedule is built as a sequence of work shifts.
    window_start = t
    window_drive_min = 0
    drive_since_break_min = 0

    def reset_shift() -> None:
        nonlocal window_start, window_drive_min, drive_since_break_min
        window_start = t
        window_drive_min = 0
        drive_since_break_min = 0

    def maybe_end_shift_for_limits() -> None:
        nonlocal window_start
        window_elapsed_min = int((t - window_start).total_seconds() // 60)
        if window_drive_min >= MAX_DRIVE_MIN or window_elapsed_min >= MAX_WINDOW_MIN:
            add("SB", int(OFF_RESET_H * 60), location=last_location, note="10-hour break (reset 11/14)")
            reset_shift()

    def maybe_break() -> None:
        nonlocal drive_since_break_min
        if drive_since_break_min >= BREAK_AFTER_DRIVE_MIN:
            add("OFF", BREAK_MIN, location=last_location, note="30-minute break")
            drive_since_break_min = 0

    def drive_minutes_for(remaining_drive_s: float) -> int:
        # Determine how much we can drive right now within rules.
        nonlocal window_start

        window_elapsed_min = int((t - window_start).total_seconds() // 60)
        drive_left_min = max(0, MAX_DRIVE_MIN - window_drive_min)
        window_left_min = max(0, MAX_WINDOW_MIN - window_elapsed_min)
        break_left_min = max(0, BREAK_AFTER_DRIVE_MIN - drive_since_break_min)
        cycle_left_min = max(0, cycle_left_minutes())

        allowed_min = min(drive_left_min, window_left_min, break_left_min, cycle_left_min)
        if allowed_min <= 0:
            return 0

        remaining_min = int(math.ceil(max(0.0, remaining_drive_s) / 60.0))
        m = min(allowed_min, remaining_min)
        return int(m) if m > 0 else 0

    def accrue_driving(minutes: int, location: str, note: str) -> None:
        nonlocal window_drive_min, drive_since_break_min, fuel_since_miles
        if minutes <= 0:
            return
        add("D", minutes, location=location, note=note)
        window_drive_min += minutes
        drive_since_break_min += minutes
        if meters_per_second > 0:
            driven_miles = (minutes * 60.0) * meters_per_second / 1609.344
            fuel_since_miles += driven_miles

    def maybe_fuel_stop(location: str) -> None:
        nonlocal fuel_since_miles
        # Insert fuel if we're at (or would exceed within the next minute of driving) the 1,000-mile interval.
        if meters_per_second > 0:
            miles_per_min = (meters_per_second * 60.0) / 1609.344
        else:
            miles_per_min = 0.0

        due = fuel_since_miles + 1e-6 >= fuel_every_miles
        if miles_per_min > 0:
            due = due or (fuel_since_miles + miles_per_min >= fuel_every_miles - 1e-6)

        if due:
            consume_cycle_or_restart(int(fuel_stop_h * 60))
            add("ON", int(fuel_stop_h * 60), location=location, note="Fuel stop (30-min break satisfied)")
            fuel_since_miles = 0.0

    # 1) Drive to pickup.
    ensure_cycle_ok()
    leg1, leg2 = legs
    remaining_s = float(leg1["duration_s"])
    while remaining_s > 0:
        # If we can't drive at least 1 minute on cycle, restart before proceeding.
        if cycle_left_minutes() <= 0:
            ensure_cycle_ok()
        # If we have no cycle minutes left, restart before continuing.
        # If we're due for fuel, insert it before continuing to drive.
        maybe_fuel_stop(last_location)
        maybe_break()
        m = drive_minutes_for(remaining_s)
        # Cap driving so we never exceed the 1,000-mile fueling interval (minute granularity).
        if m > 0 and meters_per_second > 0:
            miles_per_min = (meters_per_second * 60.0) / 1609.344
            if miles_per_min > 0:
                fuel_left = max(0.0, fuel_every_miles - fuel_since_miles)
                max_m_before_fuel = int(math.floor(fuel_left / miles_per_min + 1e-9))
                # If we can't fit at least 15 minutes before the fuel threshold,
                # force a fuel stop now (a few miles early is better than exceeding 1,000).
                if 0 < fuel_left < fuel_every_miles and max_m_before_fuel < 15:
                    m = 0
                else:
                    m = min(m, max(0, max_m_before_fuel))
        if m <= 0:
            # Ensure we always make progress (avoid infinite loops).
            prev_fuel = fuel_since_miles
            maybe_fuel_stop(last_location)
            if prev_fuel > 0 and fuel_since_miles == 0.0:
                continue

            # If we're blocked by cycle, restart now (before violating).
            if cycle_left_minutes() <= 0:
                ensure_cycle_ok()
                continue
            # With minute-level driving, a low remaining cycle will be naturally consumed.

            window_elapsed_min = int((t - window_start).total_seconds() // 60)
            if (MAX_DRIVE_MIN - window_drive_min) < 15 or (MAX_WINDOW_MIN - window_elapsed_min) < 15:
                add("OFF", int(OFF_RESET_H * 60), location=last_location, note="10-hour break (reset 11/14)")
                reset_shift()
                continue
            if (BREAK_AFTER_DRIVE_MIN - drive_since_break_min) < 15:
                add("OFF", BREAK_MIN, location=last_location, note="30-minute break")
                drive_since_break_min = 0
                continue
            if remaining_s < 15 * 60:
                remaining_s = 0
                break
            add("OFF", int(OFF_RESET_H * 60), location=last_location, note="10-hour break (reset 11/14)")
            reset_shift()
            continue
        accrue_driving(m, location="Enroute", note="Drive to pickup")
        remaining_s -= min(remaining_s, m * 60.0)
        maybe_fuel_stop(last_location)
        maybe_end_shift_for_limits()

    # Pickup service time.
    consume_cycle_or_restart(int(pickup_service_h * 60))
    add("ON", int(pickup_service_h * 60), location=pickup_label, note="Pickup (loading / paperwork)")

    # 2) Drive to dropoff.
    remaining_s = float(leg2["duration_s"])
    while remaining_s > 0:
        if cycle_left_minutes() <= 0:
            ensure_cycle_ok()
        # If we have no cycle minutes left, restart before continuing.
        maybe_fuel_stop(last_location)
        maybe_break()
        m = drive_minutes_for(remaining_s)
        if m > 0 and meters_per_second > 0:
            miles_per_min = (meters_per_second * 60.0) / 1609.344
            if miles_per_min > 0:
                fuel_left = max(0.0, fuel_every_miles - fuel_since_miles)
                max_m_before_fuel = int(math.floor(fuel_left / miles_per_min + 1e-9))
                if 0 < fuel_left < fuel_every_miles and max_m_before_fuel < 15:
                    m = 0
                else:
                    m = min(m, max(0, max_m_before_fuel))
        if m <= 0:
            prev_fuel = fuel_since_miles
            maybe_fuel_stop(last_location)
            if prev_fuel > 0 and fuel_since_miles == 0.0:
                continue

            if cycle_left_minutes() <= 0:
                ensure_cycle_ok()
                continue
            # With minute-level driving, a low remaining cycle will be naturally consumed.

            window_elapsed_min = int((t - window_start).total_seconds() // 60)
            if (MAX_DRIVE_MIN - window_drive_min) < 15 or (MAX_WINDOW_MIN - window_elapsed_min) < 15:
                add("OFF", int(OFF_RESET_H * 60), location=last_location, note="10-hour break (reset 11/14)")
                reset_shift()
                continue
            if (BREAK_AFTER_DRIVE_MIN - drive_since_break_min) < 15:
                add("OFF", BREAK_MIN, location=last_location, note="30-minute break")
                drive_since_break_min = 0
                continue
            if remaining_s < 15 * 60:
                remaining_s = 0
                break
            add("OFF", int(OFF_RESET_H * 60), location=last_location, note="10-hour break (reset 11/14)")
            reset_shift()
            continue
        accrue_driving(m, location="Enroute", note="Drive to dropoff")
        remaining_s -= min(remaining_s, m * 60.0)
        maybe_fuel_stop(last_location)
        maybe_end_shift_for_limits()

    # Dropoff service time.
    consume_cycle_or_restart(int(dropoff_service_h * 60))
    add("ON", int(dropoff_service_h * 60), location=dropoff_label, note="Dropoff (unloading / paperwork)")

    # End of trip: park and go OFF until end of current day (for clean 24h logs),
    # but don't force it if we already crossed midnight.
    t_end = t
    return {
        "segments": segments,
        "end_dt": t_end,
        "warnings": warnings,
        "cycle_used_end_h": cycle_used,
    }


def _split_segments_into_days(segments: list[Segment]) -> list[dict[str, Any]]:
    """
    Convert time segments into daily log sheets aligned to local calendar day of the segment timestamps.
    The output uses minutes-from-midnight [0..1440] segments, compatible with a graph grid.
    """

    if not segments:
        return []

    days: dict[str, dict[str, Any]] = {}

    def ensure_day(dt: datetime) -> dict[str, Any]:
        key = dt.date().isoformat()
        if key not in days:
            days[key] = {
                "date": key,
                "segments": [],  # {status,startMin,endMin}
                "remarks": [],  # {time, location, note}
                "totals": {"OFF": 0, "SB": 0, "D": 0, "ON": 0},  # minutes
            }
        return days[key]

    for s in segments:
        cur = s.start
        while cur < s.end:
            day = ensure_day(cur)
            ds = _day_start(cur)
            next_midnight = ds + timedelta(days=1)
            part_end = min(next_midnight, s.end)
            start_min = int((cur - ds).total_seconds() // 60)
            end_min = int((part_end - ds).total_seconds() // 60)
            if end_min > start_min:
                day["segments"].append({"status": s.status, "startMin": start_min, "endMin": end_min})
                day["totals"][s.status] += end_min - start_min
            cur = part_end

        # Paper-log remark: keep only meaningful events (avoid clutter from driving chunks).
        note_l = (s.note or "").lower()
        keep = s.status != "D"
        keep = keep and not note_l.startswith("off duty")  # OFF gaps are auto-filled; don't list them
        keep = keep and ("drive to" not in note_l)  # suppress drive remarks

        day = ensure_day(s.start)
        ds0 = _day_start(s.start)
        start_min = int((s.start - ds0).total_seconds() // 60)
        if keep:
            day["remarks"].append(
                {
                    "time": s.start.isoformat(),
                    "startMin": start_min,
                    "location": _paper_loc(s.location),
                    "note": s.note,
                }
            )

    # Normalize output order.
    out = [days[k] for k in sorted(days.keys())]
    for d in out:
        d["segments"].sort(key=lambda x: (x["startMin"], x["endMin"]))

        # Fill gaps so each sheet totals exactly 24 hours.
        filled: list[dict[str, Any]] = []
        cursor = 0
        for seg in d["segments"]:
            if seg["startMin"] > cursor:
                gap = {"status": "OFF", "startMin": cursor, "endMin": seg["startMin"]}
                filled.append(gap)
                d["totals"]["OFF"] += gap["endMin"] - gap["startMin"]
            filled.append(seg)
            cursor = max(cursor, int(seg["endMin"]))
        if cursor < 1440:
            gap = {"status": "OFF", "startMin": cursor, "endMin": 1440}
            filled.append(gap)
            d["totals"]["OFF"] += gap["endMin"] - gap["startMin"]
        d["segments"] = filled

        # Calculate totals in hours (decimal + hh:mm)
        totals_min = d["totals"]
        d["totalHours"] = {
            "offDuty": totals_min["OFF"] / 60.0,
            "sleeperBerth": totals_min["SB"] / 60.0,
            "driving": totals_min["D"] / 60.0,
            "onDuty": totals_min["ON"] / 60.0,
        }
        d["totalHoursPretty"] = {
            "offDuty": f"{totals_min['OFF']//60}:{totals_min['OFF']%60:02d}",
            "sleeperBerth": f"{totals_min['SB']//60}:{totals_min['SB']%60:02d}",
            "driving": f"{totals_min['D']//60}:{totals_min['D']%60:02d}",
            "onDuty": f"{totals_min['ON']//60}:{totals_min['ON']%60:02d}",
        }

    return out


def _fill_timeline_gaps(
    segments: list[Segment],
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> list[Segment]:
    if not segments:
        return []

    segs = sorted(segments, key=lambda s: (s.start, s.end))
    start = range_start or segs[0].start
    end = range_end or segs[-1].end

    out: list[Segment] = []
    cur_end = start
    last_loc = segs[0].location or "Enroute"

    for s in segs:
        if s.start > cur_end:
            out.append(Segment(status="OFF", start=cur_end, end=s.start, location=last_loc, note="Off duty"))
        out.append(s)
        cur_end = max(cur_end, s.end)
        if s.location:
            last_loc = s.location

    if cur_end < end:
        out.append(Segment(status="OFF", start=cur_end, end=end, location=last_loc, note="Off duty"))

    # Merge adjacent segments with same status/location/note for cleaner display.
    merged: list[Segment] = []
    for s in out:
        if (
            merged
            and merged[-1].status == s.status
            and merged[-1].location == s.location
            and merged[-1].note == s.note
            and merged[-1].end == s.start
        ):
            prev = merged[-1]
            merged[-1] = Segment(status=prev.status, start=prev.start, end=s.end, location=prev.location, note=prev.note)
        else:
            merged.append(s)
    return merged


@api_view(["GET"])
def health_view(_request):
    return JsonResponse({"ok": True})


@csrf_exempt
@api_view(["POST"])
def plan_trip_view(request):
    body = request.data or {}

    current_location = (body.get("currentLocation") or "").strip()
    pickup_location = (body.get("pickupLocation") or "").strip()
    dropoff_location = (body.get("dropoffLocation") or "").strip()
    current_cycle_used_h = float(body.get("currentCycleUsedHours") or 0.0)
    start_dt = _parse_start_dt(body.get("startDateTime"))

    # Optional paper-log details
    carrier_name = (body.get("carrierName") or "").strip() or "N/A"
    main_office_address = (body.get("mainOfficeAddress") or "").strip() or "N/A"
    home_terminal_address = (body.get("homeTerminalAddress") or "").strip() or current_location
    tractor_number = (body.get("tractorNumber") or "").strip() or "N/A"
    trailer_number = (body.get("trailerNumber") or "").strip() or "N/A"
    manifest_number = (body.get("manifestNumber") or "").strip()
    shipper_name = (body.get("shipperName") or "").strip()
    commodity = (body.get("commodity") or "").strip()

    if not current_location or not pickup_location or not dropoff_location:
        return JsonResponse(
            {"error": "Missing required fields: currentLocation, pickupLocation, dropoffLocation"},
            status=400,
        )
    if not (_looks_like_city_state(current_location) and _looks_like_city_state(pickup_location) and _looks_like_city_state(dropoff_location)):
        return JsonResponse(
            {"error": "Locations must be full city/state, like 'Chicago, IL'."},
            status=400,
        )

    try:
        cur = _geocode(current_location)
        pu = _geocode(pickup_location)
        do = _geocode(dropoff_location)

        routing_warnings: list[str] = []
        try:
            r1 = _route(cur, pu)
            r2 = _route(pu, do)
        except Exception as e:
            # Fallback: straight-line distance and estimated duration (lets logs still generate).
            routing_warnings.append(
                "Routing provider unavailable; using straight-line fallback route (check access to router.project-osrm.org)."
            )
            m1 = _haversine_meters(cur, pu)
            m2 = _haversine_meters(pu, do)
            # Estimate duration using 55 mph average speed.
            mph = 55.0
            r1 = {
                "distance_m": m1,
                "duration_s": (m1 / 1609.344) / mph * 3600.0,
                "geometry": {"type": "LineString", "coordinates": [[cur.lon, cur.lat], [pu.lon, pu.lat]]},
                "legs": [],
            }
            r2 = {
                "distance_m": m2,
                "duration_s": (m2 / 1609.344) / mph * 3600.0,
                "geometry": {"type": "LineString", "coordinates": [[pu.lon, pu.lat], [do.lon, do.lat]]},
                "legs": [],
            }

        legs = [
            {"distance_m": r1["distance_m"], "duration_s": r1["duration_s"]},
            {"distance_m": r2["distance_m"], "duration_s": r2["duration_s"]},
        ]

        cur_label = _short_place(getattr(cur, "label", "") or current_location) or current_location
        pu_label = _short_place(getattr(pu, "label", "") or pickup_location) or pickup_location
        do_label = _short_place(getattr(do, "label", "") or dropoff_location) or dropoff_location

        timeline = _plan_timeline(
            start_dt=start_dt,
            legs=legs,
            current_cycle_used_h=current_cycle_used_h,
            current_label=cur_label,
            pickup_label=pu_label,
            dropoff_label=do_label,
        )

        daily_logs = _split_segments_into_days(timeline["segments"])

        # Daily miles: allocate by driving time, then force the rounded day-miles to sum exactly
        # to the rounded total miles (avoids 1-mile drift from per-day rounding).
        total_drive_s = float(r1["duration_s"] + r2["duration_s"])
        total_miles_exact = (float(r1["distance_m"] + r2["distance_m"]) / 1609.344) if total_drive_s else 0.0
        total_miles_rounded = _round_half_up(total_miles_exact)

        raw_day_miles: list[float] = []
        for d in daily_logs:
            driving_h = float(d["totalHours"]["driving"])
            driving_s = driving_h * 3600.0
            raw_day_miles.append((total_miles_exact * (driving_s / total_drive_s)) if total_drive_s else 0.0)

        floored = [int(math.floor(x)) for x in raw_day_miles]
        remainder = max(0, total_miles_rounded - sum(floored))
        # Distribute remaining miles to days with the largest fractional parts.
        frac_order = sorted(range(len(raw_day_miles)), key=lambda i: (raw_day_miles[i] - math.floor(raw_day_miles[i])), reverse=True)
        for i in frac_order[:remainder]:
            floored[i] += 1

        for idx, d in enumerate(daily_logs):
            d["milesDrivingToday"] = float(floored[idx])
            d["totalMileageToday"] = d["milesDrivingToday"]

            # Paper-log header fields (auto-filled defaults; no longer blank).
            d["fromLocation"] = current_location
            d["toLocation"] = dropoff_location
            d["carrierName"] = carrier_name
            d["mainOfficeAddress"] = main_office_address
            d["homeTerminalAddress"] = home_terminal_address
            d["vehicleNumbers"] = f"Tractor: {tractor_number}  Trailer: {trailer_number}"

            d["manifestNumber"] = manifest_number or f"MAN-{d['date'].replace('-', '')}"
            if shipper_name and commodity:
                d["shipperCommodity"] = f"{shipper_name} — {commodity}"
            elif commodity:
                d["shipperCommodity"] = commodity
            elif shipper_name:
                d["shipperCommodity"] = shipper_name
            else:
                d["shipperCommodity"] = f"{pickup_location} → {dropoff_location} (General freight)"

            d["onDutyHoursToday"] = float(d["totalHours"]["driving"]) + float(d["totalHours"]["onDuty"])

        # For display, show a full log (midnight-to-midnight) across all covered days.
        range_start = _day_start(timeline["segments"][0].start)
        range_end = _day_start(timeline["segments"][-1].end) + timedelta(days=1)
        filled_timeline = _fill_timeline_gaps(timeline["segments"], range_start=range_start, range_end=range_end)

        out = {
            "inputs": {
                "currentLocation": current_location,
                "pickupLocation": pickup_location,
                "dropoffLocation": dropoff_location,
                "currentCycleUsedHours": current_cycle_used_h,
                "startDateTime": _iso(start_dt),
                "carrierName": carrier_name,
                "mainOfficeAddress": main_office_address,
                "homeTerminalAddress": home_terminal_address,
                "tractorNumber": tractor_number,
                "trailerNumber": trailer_number,
                "manifestNumber": manifest_number or None,
                "shipperName": shipper_name or None,
                "commodity": commodity or None,
            },
            "geocoding": {
                "current": {"lat": cur.lat, "lon": cur.lon, "label": cur.label},
                "pickup": {"lat": pu.lat, "lon": pu.lon, "label": pu.label},
                "dropoff": {"lat": do.lat, "lon": do.lon, "label": do.label},
            },
            "route": {
                "totalDistanceMiles": (r1["distance_m"] + r2["distance_m"]) / 1609.344,
                "totalDurationHours": (r1["duration_s"] + r2["duration_s"]) / 3600.0,
                "legs": [
                    {
                        "from": cur.label,
                        "to": pu.label,
                        "distanceMiles": r1["distance_m"] / 1609.344,
                        "durationHours": r1["duration_s"] / 3600.0,
                        "geometry": r1["geometry"],
                    },
                    {
                        "from": pu.label,
                        "to": do.label,
                        "distanceMiles": r2["distance_m"] / 1609.344,
                        "durationHours": r2["duration_s"] / 3600.0,
                        "geometry": r2["geometry"],
                    },
                ],
            },
            "timeline": [
                {
                    "status": s.status,
                    "start": _iso(s.start),
                    "end": _iso(s.end),
                    "location": s.location,
                    "note": s.note,
                    "minutes": s.minutes(),
                }
                for s in filled_timeline
            ],
            "dailyLogs": daily_logs,
            "warnings": timeline["warnings"],
            "cycleUsedEndHours": timeline["cycle_used_end_h"],
        }
        if routing_warnings:
            out["warnings"] = [*routing_warnings, *(out.get("warnings") or [])]
        return JsonResponse(out)
    except TimeoutError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": "Unexpected error: " + str(e)}, status=500)
