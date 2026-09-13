from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import (
    HARVEST_DATE_OFFSETS,
    HARVEST_DESTINATIONS,
    HARVEST_DIR,
    HARVEST_HOURS,
    HARVEST_ORIGINS,
    HARVEST_PROGRAMS,
    HARVEST_STATE_DIR,
    HARVEST_TZ,
    LATAM_MAX_PER_RUN,
    LATAM_MAX_ROUTES,
    has_live_cia_miles,
)
from app.airports import expand_city_airports
from app.catalog import collapse_rio_jobs
from app.collectors.azul import azul_session_ready, collect_azul_jobs
from app.collectors.latam import collect_latam_jobs, latam_session_ready
from app.collectors.smiles import collect_smiles_jobs, smiles_session_ready
from app.db import create_search, insert_results, now_iso, query_results, replace_miles_route, update_search

LATAM_CURSOR_PATH = HARVEST_STATE_DIR / "cursor-latam.json"
AZUL_CURSOR_PATH = HARVEST_STATE_DIR / "cursor-azul.json"
SMILES_CURSOR_PATH = HARVEST_STATE_DIR / "cursor-smiles.json"
RUNS_PATH = HARVEST_STATE_DIR / "runs.json"
ULTIMA_PATH = HARVEST_STATE_DIR / "ultima.json"
LOCK = asyncio.Lock()


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(HARVEST_TZ)
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(_tz())


def harvest_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for origin in HARVEST_ORIGINS:
        for dest in HARVEST_DESTINATIONS:
            if origin == dest:
                continue
            for program in HARVEST_PROGRAMS:
                if program not in {"smiles", "azul", "latam"}:
                    continue
                for offset in HARVEST_DATE_OFFSETS:
                    jobs.append(
                        {
                            "origin": origin,
                            "destination": dest,
                            "program": program,
                            "offset": offset,
                        }
                    )
    return collapse_rio_jobs(jobs)


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


def take_jobs(
    count: int,
    programs: list[str] | None = None,
    cursor_path: Path | None = None,
) -> list[dict[str, Any]]:
    jobs = harvest_jobs()
    if programs:
        wanted = {name.lower() for name in programs}
        jobs = [job for job in jobs if job["program"] in wanted]
    if not jobs or count <= 0:
        return []
    path = cursor_path or LATAM_CURSOR_PATH
    state = _read_json(path, {"index": 0})
    index = int(state.get("index") or 0) % len(jobs)
    chosen = []
    for step in range(min(count, len(jobs))):
        chosen.append(dict(jobs[(index + step) % len(jobs)]))
    _write_json(path, {"index": (index + len(chosen)) % len(jobs), "total": len(jobs)})
    today = date.today()
    for job in chosen:
        job["day"] = (today + timedelta(days=int(job["offset"]))).isoformat()
    return chosen


def latest_run() -> dict[str, Any] | None:
    data = _read_json(ULTIMA_PATH, None)
    return data if isinstance(data, dict) else None


def already_ran(day: str, slot: str) -> bool:
    runs = _read_json(RUNS_PATH, {})
    return slot in (runs.get(day) or [])


def mark_ran(day: str, slot: str) -> None:
    runs = _read_json(RUNS_PATH, {})
    slots = list(runs.get(day) or [])
    if slot not in slots:
        slots.append(slot)
    runs[day] = slots
    _write_json(RUNS_PATH, runs)


def next_slots(now: datetime | None = None) -> list[str]:
    current = now or now_local()
    hours = sorted(set(HARVEST_HOURS))
    upcoming = []
    for hour in hours:
        candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        upcoming.append(candidate.strftime("%d/%m %H:%M"))
    return upcoming


def harvest_status() -> dict[str, Any]:
    last = latest_run()
    return {
        "hours": list(HARVEST_HOURS),
        "max_requests": min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN),
        "origins": HARVEST_ORIGINS,
        "destinations": HARVEST_DESTINATIONS,
        "programs": HARVEST_PROGRAMS,
        "last": last,
        "next": next_slots(),
        "folder": str(HARVEST_DIR),
        "has_key": has_live_cia_miles(),
        "latam_ready": latam_session_ready(),
        "azul_ready": azul_session_ready(),
        "smiles_ready": smiles_session_ready(),
        "latam_max": min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN),
    }


def offers_from_cache(
    origins: list[str],
    destinations: list[str],
    start: date,
    end: date,
    programs: list[str],
) -> list[dict[str, Any]]:
    origin_set = {code.upper() for code in origins}
    dest_set = {code.upper() for code in destinations} if destinations else None
    program_set = {name.lower() for name in programs} if programs else None
    best: dict[tuple, dict[str, Any]] = {}
    for row in query_results(price_type="miles", sort="miles", limit=5000):
        if row.get("origin") not in origin_set:
            continue
        if dest_set is not None and row.get("destination") not in dest_set:
            continue
        program = str(row.get("miles_program") or "").lower()
        if program_set is not None and program not in program_set:
            continue
        day = str(row.get("departure_date") or "")[:10]
        if len(day) != 10:
            continue
        try:
            dep = date.fromisoformat(day)
        except ValueError:
            continue
        if dep < start or dep > end:
            continue
        key = (row["origin"], row["destination"], day, program, row.get("return_date"))
        current = best.get(key)
        if current is None or (row.get("miles") or 10**9) < (current.get("miles") or 10**9):
            best[key] = row
    return list(best.values())


def _save_ultima(local: datetime, slot: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "slot": slot,
        "label": local.strftime("%d/%m %H:%M"),
        "started_at": extra.get("started_at") or now_iso(),
        "finished_at": now_iso(),
        "offers": extra.get("offers") or 0,
        "requests": extra.get("requests") or 0,
        "stopped": extra.get("stopped"),
        **{key: value for key, value in extra.items() if key not in {"started_at", "offers", "requests", "stopped"}},
    }
    _write_json(ULTIMA_PATH, payload)
    return payload


async def run_harvest(slot: str) -> dict[str, Any]:
    async with LOCK:
        local = now_local()
        notes: list[str] = []
        latam_jobs: list[dict[str, Any]] = []
        azul_jobs: list[dict[str, Any]] = []
        smiles_jobs: list[dict[str, Any]] = []
        if "latam" in HARVEST_PROGRAMS:
            if latam_session_ready():
                latam_jobs = take_jobs(min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN), ["latam"], LATAM_CURSOR_PATH)
            else:
                notes.append("LATAM: faça login com python3 -m app.collectors.latam login")
        if "azul" in HARVEST_PROGRAMS:
            if azul_session_ready():
                azul_jobs = take_jobs(min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN), ["azul"], AZUL_CURSOR_PATH)
            else:
                notes.append("Azul: faça login com python3 -m app.collectors.azul login")
        if "smiles" in HARVEST_PROGRAMS:
            if smiles_session_ready():
                smiles_jobs = take_jobs(min(LATAM_MAX_ROUTES, LATAM_MAX_PER_RUN), ["smiles"], SMILES_CURSOR_PATH)
            else:
                notes.append("GOL/Smiles: faça login com python3 -m app.collectors.smiles login")

        all_jobs = latam_jobs + azul_jobs + smiles_jobs
        if not all_jobs:
            summary = _save_ultima(
                local,
                slot,
                planned=0,
                requests=0,
                offers=0,
                stopped="; ".join(notes) or "nada para coletar",
                jobs=[],
            )
            return {"ok": False, **summary}

        search_id = create_search(
            {
                "origins": sorted({job["origin"] for job in all_jobs}) or HARVEST_ORIGINS,
                "destinations": sorted({job["destination"] for job in all_jobs}) or HARVEST_DESTINATIONS,
                "trip_type": "one_way",
                "date_mode": "flex",
                "date_start": None,
                "date_end": None,
                "stay_nights": 7,
                "cabin": "economy",
                "include_cash": False,
                "include_miles": True,
            }
        )
        update_search(
            search_id,
            status="running",
            progress=f"Coleta {slot}: {len(latam_jobs)} LATAM + {len(azul_jobs)} Azul + {len(smiles_jobs)} GOL",
        )
        found_at = now_iso()
        saved = 0
        offers_count = 0
        stopped = None
        log: list[dict[str, Any]] = []

        def persist(job: dict[str, Any], offers: list, status: Any, file_name: str) -> None:
            nonlocal saved, offers_count
            from app.collectors.latam import plausible_miles

            valid = [
                offer
                for offer in offers
                if plausible_miles(
                    offer.miles,
                    offer.origin,
                    offer.destination,
                    offer.cabin,
                    program=job.get("program") or offer.miles_program or "latam",
                    fare=offer.fare,
                    trip_kind=offer.trip_kind,
                )
            ]
            complete = bool(valid) and status in {200, "ok", None}
            if job.get("return_day") and complete:
                complete = any((offer.trip_kind or "") in {"volta", "round_trip"} for offer in valid)
            if not complete:
                print(
                    f"Não atualizo o site {job['origin']}-{job['destination']}; mantenho a coleta anterior.",
                    flush=True,
                )
                saved += 1
                log.append(
                    {
                        "origin": job["origin"],
                        "destination": job["destination"],
                        "program": job["program"],
                        "day": job["day"],
                        "status": status,
                        "offers": 0,
                        "file": file_name,
                    }
                )
                return
            rows = [offer.as_row(search_id, found_at) for offer in valid]
            if rows:
                for origin_code in expand_city_airports(job["origin"]):
                    for dest_code in expand_city_airports(job["destination"]):
                        if origin_code != dest_code:
                            replace_miles_route(
                                origin_code,
                                dest_code,
                                job["day"],
                                job["program"],
                                return_date=job.get("return_day") or job.get("return_date"),
                            )
                insert_results(rows)
                offers_count += len(rows)
            saved += 1
            log.append(
                {
                    "origin": job["origin"],
                    "destination": job["destination"],
                    "program": job["program"],
                    "day": job["day"],
                    "status": status,
                    "offers": len(rows),
                    "file": file_name,
                }
            )

        async def run_local(title: str, jobs: list[dict[str, Any]], collect, program: str) -> None:
            nonlocal stopped
            if not jobs:
                return
            update_search(search_id, progress=f"Coleta {title} no site da cia ({len(jobs)} rotas)")
            dest = HARVEST_DIR / program
            try:
                collected = await collect(jobs, dest)
            except Exception as exc:
                notes.append(f"{title} falhou: {exc}")
                stopped = str(exc)
                return
            for item in collected:
                job = item["job"]
                file_path = Path(item.get("file") or dest)
                try:
                    relative = str(file_path.relative_to(HARVEST_DIR))
                except ValueError:
                    relative = str(file_path)
                persist(job, item.get("offers") or [], item.get("status"), relative)
                if item.get("status") in {"login", "app_error", "denied", "throttled"}:
                    reason = f"{title} parou ({item.get('status')})"
                    notes.append(reason)
                    stopped = reason
                    break

        await run_local("LATAM", latam_jobs, collect_latam_jobs, "latam")
        await run_local("Azul", azul_jobs, collect_azul_jobs, "azul")
        await run_local("GOL/Smiles", smiles_jobs, collect_smiles_jobs, "smiles")

        summary = _save_ultima(
            local,
            slot,
            started_at=found_at,
            search_id=search_id,
            planned=len(all_jobs),
            requests=saved,
            offers=offers_count,
            stopped=stopped or ("; ".join(notes) if notes else None),
            jobs=log,
        )
        if slot in {f"{hour:02d}" for hour in HARVEST_HOURS} or slot in {"00", "12"}:
            mark_ran(local.strftime("%Y-%m-%d"), slot)
        progress = f"Coleta {slot}: {offers_count} ofertas em {saved} rotas"
        if stopped:
            progress += f" · {stopped}"
        update_search(search_id, status="done", progress=progress, error=None)
        return {"ok": True, **summary}


async def harvest_scheduler() -> None:
    while True:
        try:
            local = now_local()
            day = local.strftime("%Y-%m-%d")
            for hour in HARVEST_HOURS:
                if local.hour == hour and local.minute < 8:
                    slot = f"{hour:02d}"
                    if not already_ran(day, slot) and not LOCK.locked():
                        await run_harvest(slot)
        except Exception:
            pass
        await asyncio.sleep(30)


def slot_for_now() -> str:
    return now_local().strftime("%H%M")


if __name__ == "__main__":
    async def _main() -> None:
        result = await run_harvest("cli")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(_main())
