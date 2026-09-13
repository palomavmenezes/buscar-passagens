from __future__ import annotations

import json
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.config import DATA_DIR
from app.dates import friday_monday_weekends, month_sample_dates, parse_date
from app.airports import CITY_AIRPORTS, CITY_CODE_PROGRAMS

CATALOG_PATH = DATA_DIR / "routes.json"


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    target = path or CATALOG_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def save_catalog(catalog: dict[str, Any], path: Path | None = None) -> Path:
    target = path or CATALOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def catalog_airports(catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    airports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for region in data.get("regions") or []:
        for airport in region.get("airports") or []:
            code = str(airport.get("iata") or "").upper()
            if not code or code in seen:
                continue
            seen.add(code)
            airports.append(
                {
                    "iata": code,
                    "city": airport.get("city") or code,
                    "region": region.get("id"),
                    "region_label": region.get("label"),
                    "kind": region.get("kind") or "national",
                }
            )
    return airports


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
    "italia": "italia",
    "italy": "italia",
    "espanha": "espanha",
    "spain": "espanha",
    "portugal": "portugal",
    "alemanha": "alemanha",
    "germany": "alemanha",
    "franca": "franca",
    "france": "franca",
    "estados-unidos": "estados-unidos",
    "estados unidos": "estados-unidos",
    "eua": "estados-unidos",
    "usa": "estados-unidos",
    "chile": "chile",
    "reino-unido": "reino-unido",
    "reino unido": "reino-unido",
    "uk": "reino-unido",
    "inglaterra": "reino-unido",
    "argentina": "argentina",
    "japao": "japao",
    "japan": "japao",
    "china": "china",
}


def resolve_destinations(where: str, catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    token = _fold(where)
    airports = catalog_airports(data)
    if len(token) == 3 and token.isalpha():
        code = token.upper()
        match = next((item for item in airports if item["iata"] == code), None)
        if match:
            return [match]
        return [{"iata": code, "city": code, "region": code.lower(), "region_label": code, "kind": "national"}]
    region_id = REGION_ALIASES.get(token, token)
    if region_id == "brasil":
        return [item for item in airports if item.get("kind") != "international"]
    matched = [item for item in airports if item.get("region") == region_id]
    if matched:
        return matched
    matched = [item for item in airports if _fold(str(item.get("region_label") or "")) == token]
    if matched:
        return matched
    matched = [item for item in airports if _fold(str(item.get("city") or "")) == token]
    return matched


def _leg_job(origin: str, dest: str, airport: dict[str, Any], day: date, leg: str) -> dict[str, Any]:
    return {
        "origin": origin,
        "destination": dest,
        "city": airport["city"],
        "region": airport["region"],
        "region_label": airport["region_label"],
        "kind": airport["kind"],
        "offset": 0,
        "day": day.isoformat(),
        "program": "latam",
        "leg": leg,
    }


def collapse_rio_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uma busca LATAM/Azul/GOL com RIO cobre GIG e SDU na mesma rota e datas."""
    rio = set(CITY_AIRPORTS["RIO"])

    def job_key(job: dict[str, Any]) -> tuple[str, str, str, str, str]:
        back = str(job.get("return_day") or job.get("return_date") or "")
        day = str(job.get("day") or job.get("offset") or "")
        prog = str(job.get("program") or "")
        return (job["origin"], job["destination"], day, back, prog)

    index = {job_key(job): job for job in jobs}
    used: set[tuple[str, str, str, str, str]] = set()
    collapsed: list[dict[str, Any]] = []
    for job in jobs:
        key = job_key(job)
        if key in used:
            continue
        origin, dest, day, back, prog = key
        if prog and prog not in CITY_CODE_PROGRAMS:
            used.add(key)
            collapsed.append(job)
            continue
        merged = None
        if origin in rio:
            peer = "SDU" if origin == "GIG" else "GIG"
            peer_key = (peer, dest, day, back, prog)
            if peer_key in index:
                merged = dict(job)
                merged["origin"] = "RIO"
                merged["search_airports"] = ["GIG", "SDU"]
                used.add(key)
                used.add(peer_key)
        elif dest in rio:
            peer = "SDU" if dest == "GIG" else "GIG"
            peer_key = (origin, peer, day, back, prog)
            if peer_key in index:
                merged = dict(job)
                merged["destination"] = "RIO"
                merged["search_airports"] = ["GIG", "SDU"]
                used.add(key)
                used.add(peer_key)
        if merged:
            collapsed.append(merged)
        else:
            used.add(key)
            collapsed.append(job)
    return collapsed


def requested_jobs(
    origin: str = "GIG",
    where: str | None = None,
    destinations: list[str] | None = None,
    month: str | None = None,
    dates: list[str] | None = None,
    invert: bool = True,
    weekends: bool = False,
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    start = origin.strip().upper()
    airports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in destinations or []:
        for item in resolve_destinations(token, data):
            if item["iata"] == start or item["iata"] in seen:
                continue
            seen.add(item["iata"])
            airports.append(item)
    if where:
        for item in resolve_destinations(where, data):
            if item["iata"] == start or item["iata"] in seen:
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
                jobs.append(_leg_job(start, dest, airport, friday, "ida"))
                if invert:
                    jobs.append(_leg_job(dest, start, airport, monday, "volta"))
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
            jobs.append(_leg_job(start, dest, airport, out_day, "ida"))
        for airport in airports:
            dest = airport["iata"]
            jobs.append(_leg_job(dest, start, airport, back_day, "volta"))
        return jobs
    for airport in airports:
        dest = airport["iata"]
        for day in days:
            jobs.append(_leg_job(start, dest, airport, day, "ida"))
            if invert:
                jobs.append(_leg_job(dest, start, airport, day, "volta"))
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
