from __future__ import annotations

import json
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.config import DATA_DIR
from app.dates import date_span, friday_monday_weekends, month_sample_dates, parse_date
from app.airports import AIRPORTS, BRAZIL_STATES, CITY_AIRPORTS, CITY_CODE_PROGRAMS, normalize_city_token, same_city

CATALOG_PATH = DATA_DIR / "routes.json"


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    target = path or CATALOG_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def save_catalog(catalog: dict[str, Any], path: Path | None = None) -> Path:
    target = path or CATALOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


ALL_PROGRAMS = ("latam", "azul", "smiles")


def city_skip_codes(origin: str | None) -> set[str]:
    token = normalize_city_token(origin)
    skip: set[str] = set()
    for city, members in CITY_AIRPORTS.items():
        group = {city, *members}
        if token in group:
            skip.update(group)
    return skip


def airport_programs(item: dict[str, Any]) -> tuple[str, ...]:
    raw = item.get("programs")
    if not raw:
        return ALL_PROGRAMS
    return tuple(str(name).lower() for name in raw)


def serves_program(item: dict[str, Any], program: str | None) -> bool:
    if not program:
        return True
    return program.lower() in airport_programs(item)


def place_meta(iata: str, kind: str = "national") -> dict[str, str]:
    code = (iata or "").upper()
    info = AIRPORTS.get(code) or {}
    country = str(info.get("country") or ("Brasil" if kind != "international" else ""))
    state_id, state = BRAZIL_STATES.get(code, ("", ""))
    return {
        "country": country,
        "country_id": _fold(country).replace(" ", "-"),
        "state": state,
        "state_id": state_id.upper() if state_id else "",
    }


def catalog_airports(catalog: dict[str, Any] | None = None, program: str | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    airports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for region in data.get("regions") or []:
        kind = region.get("kind") or "national"
        for airport in region.get("airports") or []:
            if not serves_program(airport, program):
                continue
            code = str(airport.get("iata") or "").upper()
            if not code or code in seen:
                continue
            seen.add(code)
            item = {
                "iata": code,
                "city": airport.get("city") or code,
                "region": region.get("id"),
                "region_label": region.get("label"),
                "kind": kind,
                "programs": list(airport_programs(airport)),
            }
            item.update(place_meta(code, kind))
            airports.append(item)
    return airports


def region_destinations(
    region: dict[str, Any],
    program: str | None = None,
    origin: str | None = None,
) -> list[tuple[str, str]]:
    origin_code = (origin or "").strip().upper()
    skip = city_skip_codes(origin_code)
    dests: list[tuple[str, str]] = []
    for airport in region.get("airports") or []:
        if not serves_program(airport, program):
            continue
        code = str(airport.get("iata") or "").upper()
        if not code or code == origin_code or code in skip:
            continue
        dests.append((code, str(airport.get("city") or code)))
    return dests


def catalog_pairs(catalog: dict[str, Any] | None = None) -> list[dict[str, str]]:
    data = catalog or load_catalog()
    origins = [code.strip().upper() for code in data.get("origins") or ["GIG"] if code.strip()]
    invert = bool(data.get("invert", True))
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for origin in origins:
        for airport in catalog_airports(data):
            dest = airport["iata"]
            if origin == dest:
                continue
            directions = [(origin, dest)]
            if invert:
                directions.append((dest, origin))
            for left, right in directions:
                key = (left, right)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(
                    {
                        "origin": left,
                        "destination": right,
                        "city": airport["city"],
                        "region": airport["region"],
                        "region_label": airport["region_label"],
                        "kind": airport["kind"],
                    }
                )
    return pairs


def catalog_jobs(catalog: dict[str, Any] | None = None, today: date | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    offsets = [int(item) for item in data.get("date_offsets") or [30, 90]]
    start = today or date.today()
    jobs: list[dict[str, Any]] = []
    for pair in catalog_pairs(data):
        for offset in offsets:
            job = dict(pair)
            job["offset"] = offset
            job["day"] = (start + timedelta(days=offset)).isoformat()
            job["program"] = "latam"
            jobs.append(job)
    return jobs


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


REGION_ALIASES = {
    "nordeste": "nordeste",
    "ne": "nordeste",
    "norte": "norte",
    "sul": "sul",
    "sudeste": "sudeste",
    "centro-oeste": "centro-oeste",
    "centrooeste": "centro-oeste",
    "centro": "centro-oeste",
    "brasil": "brasil",
    "nacional": "brasil",
    "nacionais": "brasil",
    "internacional": "internacional",
    "internacionais": "internacional",
    "international": "internacional",
    "europa": "europa",
    "europe": "europa",
    "italia": "europa",
    "italy": "europa",
    "espanha": "europa",
    "spain": "europa",
    "portugal": "europa",
    "alemanha": "europa",
    "germany": "europa",
    "franca": "europa",
    "france": "europa",
    "reino-unido": "europa",
    "reino unido": "europa",
    "uk": "europa",
    "inglaterra": "europa",
    "belgica": "europa",
    "holanda": "europa",
    "paises baixos": "europa",
    "america-norte": "america-norte",
    "america do norte": "america-norte",
    "america-do-norte": "america-norte",
    "estados-unidos": "america-norte",
    "estados unidos": "america-norte",
    "eua": "america-norte",
    "usa": "america-norte",
    "mexico": "america-norte",
    "america-sul": "america-sul",
    "america do sul": "america-sul",
    "america-do-sul": "america-sul",
    "chile": "america-sul",
    "argentina": "america-sul",
    "peru": "america-sul",
    "colombia": "america-sul",
    "paraguai": "america-sul",
    "uruguai": "america-sul",
    "america-central": "america-central",
    "america central": "america-central",
    "caribe": "america-central",
    "asia": "asia",
    "japao": "asia",
    "japan": "asia",
    "china": "asia",
    "oriente-medio": "oriente-medio",
    "oriente medio": "oriente-medio",
    "africa": "africa",
}


AREA_AIRPORTS = {
    "italia": ["FCO", "MXP"],
    "italy": ["FCO", "MXP"],
    "espanha": ["MAD", "BCN"],
    "spain": ["MAD", "BCN"],
    "portugal": ["LIS", "OPO"],
    "alemanha": ["FRA", "BER", "MUC"],
    "germany": ["FRA", "BER", "MUC"],
    "franca": ["CDG"],
    "france": ["CDG"],
    "reino-unido": ["LHR"],
    "reino unido": ["LHR"],
    "uk": ["LHR"],
    "inglaterra": ["LHR"],
    "belgica": ["BRU"],
    "holanda": ["AMS"],
    "paises baixos": ["AMS"],
    "estados-unidos": ["MIA", "JFK", "MCO", "LAX", "FLL"],
    "estados unidos": ["MIA", "JFK", "MCO", "LAX", "FLL"],
    "eua": ["MIA", "JFK", "MCO", "LAX", "FLL"],
    "usa": ["MIA", "JFK", "MCO", "LAX", "FLL"],
    "mexico": ["MEX", "CUN"],
    "chile": ["SCL"],
    "argentina": ["EZE", "BRC", "COR", "MDZ"],
    "peru": ["LIM", "CUZ"],
    "colombia": ["BOG", "CTG"],
    "paraguai": ["ASU"],
    "uruguai": ["MVD", "PDP"],
    "curacao": ["CUR"],
    "caribe": ["CUR", "CUN", "PUJ"],
    "japao": ["NRT"],
    "japan": ["NRT"],
    "china": ["PEK", "PVG"],
}


def resolve_destinations(
    where: str,
    catalog: dict[str, Any] | None = None,
    program: str | None = None,
) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    token = _fold(where)
    city = normalize_city_token(where)
    if city in CITY_AIRPORTS:
        info = CITY_AIRPORTS[city]
        label = "Rio de Janeiro" if city == "RIO" else "São Paulo"
        return [
            {
                "iata": city,
                "city": label,
                "region": "sudeste",
                "region_label": "Sudeste",
                "kind": "national",
                "programs": ["latam", "azul", "smiles"],
                "search_airports": list(info),
            }
        ]
    airports = catalog_airports(data, program=program)
    if token in AREA_AIRPORTS:
        wanted = {code.upper() for code in AREA_AIRPORTS[token]}
        return [item for item in airports if item["iata"] in wanted]
    if len(token) == 3 and token.isalpha():
        code = token.upper()
        match = next((item for item in airports if item["iata"] == code), None)
        if match:
            return [match]
        return [{"iata": code, "city": code, "region": code.lower(), "region_label": code, "kind": "national"}]
    region_id = REGION_ALIASES.get(token, token)
    if region_id == "brasil":
        return [item for item in airports if item.get("kind") != "international"]
    if region_id in {"internacional", "internacionais", "international"}:
        return [item for item in airports if item.get("kind") == "international"]
    matched = [item for item in airports if item.get("region") == region_id]
    if matched:
        return matched
    matched = [item for item in airports if _fold(str(item.get("region_label") or "")) == token]
    if matched:
        return matched
    matched = [item for item in airports if _fold(str(item.get("city") or "")) == token]
    return matched


def _leg_job(origin: str, dest: str, airport: dict[str, Any], day: date, leg: str, program: str = "latam") -> dict[str, Any]:
    return {
        "origin": origin,
        "destination": dest,
        "city": airport["city"],
        "region": airport["region"],
        "region_label": airport["region_label"],
        "kind": airport["kind"],
        "offset": 0,
        "day": day.isoformat(),
        "program": program,
        "leg": leg,
    }


def _round_job(
    origin: str,
    dest: str,
    airport: dict[str, Any],
    day: date,
    return_day: date,
    program: str = "latam",
) -> dict[str, Any]:
    job = _leg_job(origin, dest, airport, day, "ida", program)
    job["return_day"] = return_day.isoformat()
    return job


def collapse_city_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uma busca LATAM/Azul com RIO ou SAO cobre todos os aeroportos da cidade."""

    def job_key(job: dict[str, Any]) -> tuple[str, str, str, str, str]:
        back = str(job.get("return_day") or job.get("return_date") or "")
        day = str(job.get("day") or job.get("offset") or "")
        prog = str(job.get("program") or "")
        return (job["origin"], job["destination"], day, back, prog)

    used: set[tuple[str, str, str, str, str]] = set()
    collapsed: list[dict[str, Any]] = []
    remaining = [job for job in jobs if job]

    def unused(job: dict[str, Any]) -> bool:
        return job_key(job) not in used

    for city, members in CITY_AIRPORTS.items():
        member_set = set(members)
        groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for job in remaining:
            if not unused(job):
                continue
            prog = str(job.get("program") or "")
            if prog and prog not in CITY_CODE_PROGRAMS:
                continue
            day = str(job.get("day") or job.get("offset") or "")
            back = str(job.get("return_day") or job.get("return_date") or "")
            origin = job["origin"]
            dest = job["destination"]
            if origin in member_set:
                groups.setdefault(("out", dest, day, back, prog), []).append(job)
            elif dest in member_set:
                groups.setdefault(("in", origin, day, back, prog), []).append(job)
        for (side, other, day, back, prog), group in groups.items():
            codes = {
                (item["origin"] if side == "out" else item["destination"])
                for item in group
            }
            if len(codes) < 2:
                continue
            merged = dict(group[0])
            if side == "out":
                merged["origin"] = city
            else:
                merged["destination"] = city
            merged["search_airports"] = list(members)
            if city == "SAO":
                merged["city"] = "São Paulo"
            elif city == "RIO":
                merged["city"] = "Rio de Janeiro"
            collapsed.append(merged)
            for item in group:
                used.add(job_key(item))

    for job in remaining:
        key = job_key(job)
        if key in used:
            continue
        used.add(key)
        collapsed.append(job)
    return collapsed


def collapse_rio_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return collapse_city_jobs(jobs)


def requested_jobs(
    origin: str = "GIG",
    where: str | None = None,
    destinations: list[str] | None = None,
    month: str | None = None,
    dates: list[str] | None = None,
    invert: bool = True,
    weekends: bool = False,
    catalog: dict[str, Any] | None = None,
    program: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    return_from: str | None = None,
    return_to: str | None = None,
) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    start = normalize_city_token(origin) or origin.strip().upper()
    prog = (program or "latam").lower()
    airports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in destinations or []:
        for item in resolve_destinations(token, data, program=prog):
            if same_city(item["iata"], start) or item["iata"] in seen:
                continue
            seen.add(item["iata"])
            airports.append(item)
    if where:
        for item in resolve_destinations(where, data, program=prog):
            if same_city(item["iata"], start) or item["iata"] in seen:
                continue
            seen.add(item["iata"])
            airports.append(item)
    jobs: list[dict[str, Any]] = []
    if weekends:
        if not month:
            return []
        for airport in airports:
            dest = airport["iata"]
            for friday, monday in friday_monday_weekends(month):
                if invert:
                    jobs.append(_round_job(start, dest, airport, friday, monday, prog))
                else:
                    jobs.append(_leg_job(start, dest, airport, friday, "ida", prog))
        return jobs
    out_days = date_span(date_from, date_to, max_days=180)
    back_days = date_span(return_from, return_to, max_days=180)
    if out_days and back_days:
        for airport in airports:
            dest = airport["iata"]
            for out_day, back_day in zip(out_days, back_days):
                jobs.append(_round_job(start, dest, airport, out_day, back_day, prog))
        return jobs
    if out_days or back_days:
        for airport in airports:
            dest = airport["iata"]
            for day in out_days:
                jobs.append(_leg_job(start, dest, airport, day, "ida", prog))
            for day in back_days:
                jobs.append(_leg_job(dest, start, airport, day, "volta", prog))
        return jobs
    days: list[date] = []
    for raw in dates or []:
        parsed = parse_date(raw[:10]) if raw else None
        if parsed and parsed not in days:
            days.append(parsed)
    if month:
        for parsed in month_sample_dates(month):
            if parsed not in days:
                days.append(parsed)
    if not days:
        days = [date.today() + timedelta(days=30)]
    if invert and len(days) >= 2:
        out_day, back_day = days[0], days[-1]
        for airport in airports:
            dest = airport["iata"]
            jobs.append(_round_job(start, dest, airport, out_day, back_day, prog))
        return jobs
    for airport in airports:
        dest = airport["iata"]
        for day in days:
            jobs.append(_leg_job(start, dest, airport, day, "ida", prog))
            if invert:
                jobs.append(_leg_job(dest, start, airport, day, "volta", prog))
    return jobs


def snapshot_queue(catalog: dict[str, Any] | None = None, today: date | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    jobs = catalog_jobs(data, today)
    origins = {code.strip().upper() for code in data.get("origins") or ["GIG"] if code.strip()}
    if data.get("outbound_only", True):
        jobs = [job for job in jobs if job["origin"] in origins]
    if data.get("one_offset_per_run", True):
        first = int((data.get("date_offsets") or [30])[0])
        jobs = [job for job in jobs if int(job["offset"]) == first]
    return jobs


def add_origin(code: str) -> dict[str, Any]:
    catalog = load_catalog()
    origins = [item.upper() for item in catalog.get("origins") or []]
    token = code.strip().upper()
    if token and token not in origins:
        origins.append(token)
    catalog["origins"] = origins
    save_catalog(catalog)
    return catalog


def add_airport(region_id: str, iata: str, city: str, label: str | None = None, kind: str = "national") -> dict[str, Any]:
    catalog = load_catalog()
    regions = catalog.setdefault("regions", [])
    region = next((item for item in regions if item.get("id") == region_id), None)
    if region is None:
        region = {"id": region_id, "label": label or region_id, "kind": kind, "airports": []}
        regions.append(region)
    airports = region.setdefault("airports", [])
    code = iata.strip().upper()
    if code and not any(str(item.get("iata") or "").upper() == code for item in airports):
        airports.append({"iata": code, "city": city})
    save_catalog(catalog)
    return catalog
