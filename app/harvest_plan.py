from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.catalog import (
    AREA_AIRPORTS,
    catalog_airports,
    collapse_rio_jobs,
    load_catalog,
    region_destinations,
    resolve_destinations,
    serves_program,
    _fold,
)
from app.airports import normalize_city_token, same_city
from app.config import (
    HARVEST_ORIGIN,
    HARVEST_STATE_DIR,
    HARVEST_TZ,
    LATAM_MAX_PER_RUN,
    LATAM_MAX_ROUTES,
)

AGENDA_PATH = HARVEST_STATE_DIR / "agenda.json"
CURSOR_PATH = HARVEST_STATE_DIR / "cursor-region.json"

REGIONS = (
    {"id": "nordeste", "kind": "national", "label": "Nordeste"},
    {"id": "sul", "kind": "national", "label": "Sul"},
    {"id": "sudeste", "kind": "national", "label": "Sudeste"},
    {"id": "italia", "kind": "international", "label": "Itália"},
    {"id": "franca", "kind": "international", "label": "França"},
    {"id": "espanha", "kind": "international", "label": "Espanha"},
    {"id": "portugal", "kind": "international", "label": "Portugal"},
)
REGION_BY_ID = {item["id"]: item for item in REGIONS}
NATIONAL_REGIONS = tuple(item["id"] for item in REGIONS if item["kind"] == "national")
INTERNATIONAL_REGIONS = tuple(item["id"] for item in REGIONS if item["kind"] == "international")
INTERNATIONAL_IATAS = {
    "italia": ("FCO", "MXP"),
    "franca": ("CDG",),
    "espanha": ("MAD", "BCN"),
    "portugal": ("LIS", "OPO"),
}
KIND_ALIASES = {
    "national": "national",
    "nacional": "national",
    "nacionais": "national",
    "international": "international",
    "internacional": "international",
    "internacionais": "international",
    "both": "both",
    "ambos": "both",
}

BUCKETS = {
    "madrugada": ((0, 25), (5, 40)),
    "tarde": ((13, 5), (17, 45)),
    "noite": ((19, 10), (23, 20)),
}
MIN_GAP = timedelta(hours=6)
SLOTS_PER_REGION = 2
HORIZON_DAYS = 14


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(HARVEST_TZ)
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(_tz())


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz())
    return parsed.astimezone(_tz())


def _random_in_bucket(day: date, bucket: str) -> datetime:
    (h1, m1), (h2, m2) = BUCKETS[bucket]
    start = datetime(day.year, day.month, day.day, h1, m1, tzinfo=_tz())
    end = datetime(day.year, day.month, day.day, h2, m2, tzinfo=_tz())
    span = max(60, int((end - start).total_seconds()))
    return start + timedelta(seconds=random.randint(0, span))


def region_choices() -> list[dict[str, str]]:
    return [{"id": item["id"], "label": item["label"], "kind": item["kind"]} for item in REGIONS]


def harvest_picker() -> dict[str, list[dict[str, str]]]:
    catalog = load_catalog()
    airports = catalog_airports(catalog)
    regions = [
        {
            "id": str(region.get("id") or ""),
            "label": str(region.get("label") or region.get("id") or ""),
            "kind": str(region.get("kind") or "national"),
        }
        for region in catalog.get("regions") or []
        if region.get("id")
    ]
    countries: dict[str, str] = {}
    states: dict[str, str] = {}
    for item in airports:
        if item.get("kind") == "international" and item.get("country_id") and item.get("country"):
            countries[str(item["country_id"])] = str(item["country"])
        if item.get("kind") != "international" and item.get("state_id") and item.get("state"):
            states[str(item["state_id"])] = str(item["state"])
    city_codes = [
        {"id": "RIO", "label": "RIO · Rio de Janeiro (GIG + SDU)"},
        {"id": "SAO", "label": "SAO · São Paulo (CGH + GRU + VCP)"},
    ]
    airport_opts = city_codes + [
        {"id": item["iata"], "label": f"{item['iata']} · {item['city']}"}
        for item in sorted(airports, key=lambda row: (row.get("city") or "", row["iata"]))
        if item["iata"] not in {"GIG", "SDU"}
    ]
    return {
        "regions": regions,
        "countries": [{"id": key, "label": label} for key, label in sorted(countries.items(), key=lambda row: row[1])],
        "states": [
            {"id": key, "label": f"{label} ({key})"}
            for key, label in sorted(states.items(), key=lambda row: row[1])
        ],
        "airports": airport_opts,
    }


def airports_for_scope(scope: str, target: str) -> list[dict[str, Any]]:
    token = (target or "").strip()
    if not token:
        return []
    kind = (scope or "").strip().lower()
    airports = catalog_airports()
    if kind == "airport":
        return resolve_destinations(token)
    if kind == "region":
        region_id = normalize_region(token) or token.lower()
        if region_id in REGION_BY_ID:
            return _airports_for_region(region_id)
        matched = [item for item in airports if item.get("region") == token or item.get("region") == region_id]
        if matched:
            return matched
        return resolve_destinations(token)
    if kind == "country":
        folded = _fold(token).replace(" ", "-")
        matched = [
            item
            for item in airports
            if item.get("country_id") == folded or _fold(str(item.get("country") or "")).replace(" ", "-") == folded
        ]
        if matched:
            return matched
        if folded in AREA_AIRPORTS or token.lower() in AREA_AIRPORTS:
            return resolve_destinations(token)
        return resolve_destinations(token)
    if kind == "state":
        key = token.upper()
        folded = _fold(token)
        return [
            item
            for item in airports
            if item.get("state_id") == key or _fold(str(item.get("state") or "")) == folded
        ]
    return []


def normalize_region(value: str | None) -> str | None:
    token = (value or "").strip().lower()
    if not token:
        return None
    folded = (
        token.replace("á", "a")
        .replace("â", "a")
        .replace("ã", "a")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ô", "o")
        .replace("ú", "u")
        .replace("ç", "c")
    )
    aliases = {
        "ne": "nordeste",
        "italy": "italia",
        "italyia": "italia",
        "france": "franca",
        "spain": "espanha",
    }
    token = aliases.get(folded, folded)
    return token if token in REGION_BY_ID else None


def normalize_kind(value: str | None) -> str | None:
    token = (value or "").strip().lower()
    return KIND_ALIASES.get(token)


def _load_agenda() -> dict[str, Any]:
    data = _read_json(AGENDA_PATH, {})
    if not isinstance(data, dict):
        return {"days": {}}
    data.setdefault("days", {})
    return data


def _flatten_slots(agenda: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in sorted(agenda.get("days") or {}):
        for slot in agenda["days"][key].get("slots") or []:
            region = slot.get("region")
            if region not in REGION_BY_ID:
                continue
            when = _parse_when(slot.get("when") or "")
            if when is None:
                continue
            item = dict(slot)
            item["kind"] = REGION_BY_ID[region]["kind"]
            item["when_dt"] = when
            found.append(item)
    found.sort(key=lambda item: item["when_dt"])
    return found


def _conflicts(when: datetime, occupied: list[datetime]) -> bool:
    for other in occupied:
        gap = when - other if when >= other else other - when
        if gap < MIN_GAP:
            return True
        if when.date() == other.date() and when.hour == other.hour:
            return True
    return False


def _place_slot(
    region: dict[str, str],
    today: date,
    occupied: list[datetime],
    busy_days: set[str],
) -> dict[str, Any] | None:
    days = [today + timedelta(days=offset) for offset in range(HORIZON_DAYS)]
    random.shuffle(days)
    buckets = list(BUCKETS)
    for day in days:
        key = day.isoformat()
        if key in busy_days and random.random() < 0.72:
            continue
        random.shuffle(buckets)
        for bucket in buckets:
            when = _random_in_bucket(day, bucket)
            if _conflicts(when, occupied):
                when = when + MIN_GAP + timedelta(minutes=random.randint(8, 70))
            if when.date() < today:
                continue
            if when.date() > today + timedelta(days=HORIZON_DAYS - 1):
                continue
            if _conflicts(when, occupied):
                continue
            return {
                "id": f"{when.strftime('%Y%m%d-%H%M')}-{region['id'][:3]}",
                "region": region["id"],
                "kind": region["kind"],
                "when": when.isoformat(timespec="minutes"),
                "when_dt": when,
            }
    return None


def _store_slots(slots: list[dict[str, Any]]) -> dict[str, Any]:
    days: dict[str, Any] = {}
    for slot in sorted(slots, key=lambda item: item["when_dt"]):
        key = slot["when_dt"].date().isoformat()
        stored = {
            "id": slot["id"],
            "region": slot["region"],
            "kind": slot["kind"],
            "when": slot["when_dt"].isoformat(timespec="minutes"),
        }
        if slot.get("done"):
            stored["done"] = True
        days.setdefault(key, {"skip": False, "slots": []})
        days[key]["slots"].append(stored)
    today = now_local().date()
    for offset in range(HORIZON_DAYS):
        key = (today + timedelta(days=offset)).isoformat()
        days.setdefault(key, {"skip": True, "slots": []})
    agenda = {"days": days}
    _write_json(AGENDA_PATH, agenda)
    return agenda


def ensure_agenda(horizon_days: int = HORIZON_DAYS) -> dict[str, Any]:
    del horizon_days  # horizon is fixed so skips and gaps stay predictable
    agenda = _load_agenda()
    today = now_local().date()
    start = datetime(today.year, today.month, today.day, tzinfo=_tz())
    kept: list[dict[str, Any]] = []
    for slot in _flatten_slots(agenda):
        if slot["when_dt"] < start - timedelta(hours=6):
            continue
        kept.append(slot)
    occupied = [slot["when_dt"] for slot in kept if not slot.get("done")]
    busy_days = {slot["when_dt"].date().isoformat() for slot in kept if not slot.get("done")}
    for region in REGIONS:
        pending = [
            slot
            for slot in kept
            if slot.get("region") == region["id"] and not slot.get("done") and slot["when_dt"] >= start
        ]
        need = SLOTS_PER_REGION - len(pending)
        for _ in range(max(0, need)):
            placed = _place_slot(region, today, occupied, busy_days)
            if placed is None:
                break
            kept.append(placed)
            occupied.append(placed["when_dt"])
            busy_days.add(placed["when_dt"].date().isoformat())
    return _store_slots(kept)


def upcoming_slots(limit: int = 8, now: datetime | None = None) -> list[dict[str, Any]]:
    ensure_agenda()
    current = now or now_local()
    found: list[dict[str, Any]] = []
    for slot in _flatten_slots(_load_agenda()):
        if slot.get("done"):
            continue
        if slot["when_dt"] + timedelta(minutes=25) < current:
            continue
        found.append(slot)
        if len(found) >= limit:
            break
    return found


def due_slots(now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or now_local()
    due = []
    for slot in upcoming_slots(limit=6, now=current):
        when = slot["when_dt"]
        if when <= current <= when + timedelta(minutes=25):
            due.append(slot)
    return due


def mark_slot_done(slot_id: str) -> None:
    agenda = _load_agenda()
    for day in agenda["days"].values():
        for slot in day.get("slots") or []:
            if slot.get("id") == slot_id:
                slot["done"] = True
    _write_json(AGENDA_PATH, agenda)


def format_slot_label(slot: dict[str, Any]) -> str:
    when = slot.get("when_dt") or _parse_when(slot.get("when") or "")
    stamp = when.strftime("%d/%m %H:%M") if when else "?"
    info = REGION_BY_ID.get(slot.get("region") or "", {})
    label = info.get("label") or slot.get("region") or slot.get("kind") or ""
    return f"{stamp} {label}"


def next_region_id(kind: str | None = None) -> str:
    wanted = [item["id"] for item in REGIONS if kind is None or item["kind"] == kind]
    if not wanted:
        wanted = [item["id"] for item in REGIONS]
    state = _read_json(CURSOR_PATH, {})
    key = f"next:{kind or 'any'}"
    index = int(state.get(key) or 0) % len(wanted)
    picked = wanted[index]
    state[key] = (index + 1) % len(wanted)
    _write_json(CURSOR_PATH, state)
    return picked


def _airports_for_region(region_id: str) -> list[dict[str, Any]]:
    info = REGION_BY_ID.get(region_id)
    if info is None:
        return []
    catalog = load_catalog()
    origin = HARVEST_ORIGIN
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    wanted = set(INTERNATIONAL_IATAS.get(region_id) or ())
    for region in catalog.get("regions") or []:
        catalog_id = str(region.get("id") or "")
        if info["kind"] == "national" and catalog_id != region_id:
            continue
        if info["kind"] == "international" and region.get("kind") != "international":
            continue
        for code, city in region_destinations(region, origin=origin):
            if wanted and code not in wanted:
                continue
            if code in seen:
                continue
            seen.add(code)
            airport = next(
                (item for item in (region.get("airports") or []) if str(item.get("iata") or "").upper() == code),
                {},
            )
            picked.append(
                {
                    "iata": code,
                    "city": city,
                    "region": region_id,
                    "region_label": info["label"],
                    "kind": info["kind"],
                    "programs": list(name.lower() for name in (airport.get("programs") or ["latam", "azul"])),
                }
            )
    return picked


def _pick_dates(outbound: str | None = None, inbound: str | None = None) -> tuple[str, str | None]:
    day = None
    if outbound:
        try:
            day = date.fromisoformat(outbound[:10])
        except ValueError:
            day = None
    if day is None:
        day = date.today() + timedelta(days=random.randint(18, 78))
    back = None
    if inbound:
        try:
            back = date.fromisoformat(inbound[:10])
        except ValueError:
            back = None
    if back is None:
        back = day + timedelta(days=random.randint(4, 10))
    if back <= day:
        back = day + timedelta(days=random.randint(4, 9))
    return day.isoformat(), back.isoformat()


def _jobs_for_airports(
    airports: list[dict[str, Any]],
    program: str,
    origin: str,
    day: str,
    return_day: str | None,
    cap: int,
    cursor_key: str,
) -> list[dict[str, Any]]:
    program = program.lower()
    eligible = [
        item
        for item in airports
        if serves_program({"programs": item.get("programs")}, program) and item["iata"] != origin
    ]
    if not eligible or cap <= 0:
        return []
    state = _read_json(CURSOR_PATH, {})
    index = int(state.get(cursor_key) or 0) % len(eligible)
    ordered = eligible[index:] + eligible[:index]
    take_n = min(cap, len(ordered))
    chosen = ordered[:take_n]
    if take_n >= 3 and random.random() < 0.4:
        chosen = [item for i, item in enumerate(chosen) if i != random.randrange(len(chosen))]
    state[cursor_key] = (index + max(1, len(chosen))) % len(eligible)
    _write_json(CURSOR_PATH, state)
    jobs = []
    for airport in chosen:
        job = {
            "origin": origin,
            "destination": airport["iata"],
            "city": airport["city"],
            "region": airport.get("region"),
            "region_label": airport.get("region_label"),
            "kind": airport.get("kind") or "national",
            "program": program,
            "day": day,
            "leg": "ida",
        }
        if return_day:
            job["return_day"] = return_day
        jobs.append(job)
    return jobs


def take_region_jobs(
    region_id: str,
    program: str,
    count: int | None = None,
    origin: str | None = None,
    day: str | None = None,
    return_day: str | None = None,
) -> list[dict[str, Any]]:
    region_id = normalize_region(region_id) or region_id
    if region_id in REGION_BY_ID:
        airports = _airports_for_region(region_id)
        kind = REGION_BY_ID[region_id]["kind"]
        cap = count or min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN, 4 if kind == "national" else 3)
    else:
        airports = airports_for_scope("region", region_id)
        kind = (airports[0].get("kind") if airports else "national") or "national"
        cap = count or min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN, 4 if kind == "national" else 3)
    if not airports:
        return []
    cap = max(1, min(int(cap), 6))
    outbound, inbound = _pick_dates(day, return_day)
    return _jobs_for_airports(
        airports,
        program,
        (origin or HARVEST_ORIGIN).upper(),
        outbound,
        inbound,
        cap,
        f"{region_id}:{program.lower()}",
    )


def take_target_jobs(
    scope: str,
    target: str,
    program: str,
    count: int | None = None,
    origin: str | None = None,
    day: str | None = None,
    return_day: str | None = None,
) -> list[dict[str, Any]]:
    airports = airports_for_scope(scope, target)
    if not airports:
        return []
    kind = (airports[0].get("kind") if airports else "national") or "national"
    default_cap = 1 if (scope or "").lower() == "airport" else (4 if kind == "national" else 3)
    cap = count or min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN, default_cap)
    cap = max(1, min(int(cap), 6))
    outbound, inbound = _pick_dates(day, return_day)
    return _jobs_for_airports(
        airports,
        program,
        (origin or HARVEST_ORIGIN).upper(),
        outbound,
        inbound,
        cap,
        f"{scope}:{target}:{program.lower()}",
    )


def take_kind_jobs(
    kind: str,
    program: str,
    count: int | None = None,
) -> list[dict[str, Any]]:
    kind = normalize_kind(kind) or "national"
    if kind == "both":
        return take_region_jobs(next_region_id("national"), program, count) + take_region_jobs(
            next_region_id("international"), program, count
        )
    region_id = next_region_id("international" if kind == "international" else "national")
    return take_region_jobs(region_id, program, count)


def jobs_from_request(
    origin: str,
    destination: str,
    trip_type: str = "one_way",
    date_start: str | None = None,
    date_end: str | None = None,
    programs: list[str] | None = None,
) -> list[dict[str, Any]]:
    start = normalize_city_token(origin or HARVEST_ORIGIN) or HARVEST_ORIGIN
    token = (destination or "").strip()
    if not token:
        return []
    wanted = [name.lower() for name in (programs or ["latam"]) if name.lower() in {"latam", "azul"}]
    if not wanted:
        wanted = ["latam"]
    round_trip = trip_type == "round_trip"
    outbound, inbound = _pick_dates(date_start, date_end if round_trip else None)
    if not round_trip:
        inbound = None
    airports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in resolve_destinations(token):
        code = str(item.get("iata") or "").upper()
        if not code or same_city(code, start) or code in seen:
            continue
        seen.add(code)
        airports.append(item)
    if not airports:
        code = normalize_city_token(token)
        if len(code) == 3 and code.isalpha() and not same_city(code, start):
            airports.append(
                {
                    "iata": code,
                    "city": code,
                    "region": "",
                    "region_label": code,
                    "kind": "national",
                    "programs": ["latam", "azul"],
                }
            )
    cap = min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN, 6)
    jobs: list[dict[str, Any]] = []
    for airport in airports:
        for program in wanted:
            if airport.get("programs") and not serves_program(airport, program):
                continue
            job = {
                "origin": start,
                "destination": airport["iata"],
                "city": airport.get("city") or airport["iata"],
                "region": airport.get("region"),
                "region_label": airport.get("region_label"),
                "kind": airport.get("kind") or "national",
                "program": program,
                "day": outbound,
                "leg": "ida",
            }
            if inbound:
                job["return_day"] = inbound
            jobs.append(job)
            if len(jobs) >= cap:
                return collapse_rio_jobs(jobs)
    return collapse_rio_jobs(jobs)
