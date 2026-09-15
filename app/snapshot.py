from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.airports import CITY_AIRPORTS, city_name, expand_city_airports, normalize_city_token, related_airports, same_city
from app.catalog import catalog_airports, collapse_rio_jobs, load_catalog, requested_jobs, snapshot_queue
from app.collectors.azul import azul_session_ready, collect_azul_jobs
from app.collectors.latam import collect_latam_jobs, latam_session_ready
from app.collectors.smiles import collect_smiles_jobs, smiles_session_ready
from app.config import HARVEST_DIR, LATAM_MAX_PER_RUN, harvest_route_path
from app.db import create_search, init_db, insert_results, now_iso, query_results, replace_miles_route, update_search
from app.miles_budget import plausible_miles, smiles_keep_offer
from app.providers.base import Offer


def _job_identity(job: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        job["origin"],
        job["destination"],
        job["day"],
        job.get("return_day") or job.get("return_date") or "",
    )


def _payload_covers_job(payload: dict[str, Any], job: dict[str, Any]) -> bool:
    status = payload.get("status")
    offers = payload.get("offers") or []
    if status == "empty":
        pass
    elif status not in {200, "ok"} or not offers:
        return False
    saved = payload.get("job") or {}
    if str(saved.get("day") or job["day"]) != str(job["day"]):
        return False
    job_back = str(job.get("return_day") or job.get("return_date") or "")
    saved_back = str(saved.get("return_day") or saved.get("return_date") or "")
    if job_back and saved_back and saved_back != job_back:
        return False
    saved_origin = str(saved.get("origin") or "")
    saved_dest = str(saved.get("destination") or "")
    if saved_origin and not same_city(job["origin"], saved_origin):
        return False
    if saved_dest and not same_city(job["destination"], saved_dest):
        return False
    job_search = (job.get("search_origin") or job["origin"]).upper()
    saved_search = str(saved.get("search_origin") or "").upper()
    if job_search in CITY_AIRPORTS:
        if saved_search:
            return saved_search == job_search
        return str(saved.get("origin") or "").upper() == job_search
    return True


def pending_jobs(jobs: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
    done: set[tuple[str, str, str, str]] = set()
    for job in jobs:
        origins = related_airports(job["origin"]) or [job["origin"]]
        dests = related_airports(job["destination"]) or [job["destination"]]
        covered = False
        for origin in origins:
            for dest in dests:
                if origin.upper() == dest.upper():
                    continue
                path = harvest_route_path(
                    folder,
                    origin,
                    dest,
                    job["day"],
                    job={**job, "origin": origin, "destination": dest},
                )
                if not path.exists():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if _payload_covers_job(payload, job):
                    covered = True
                    break
            if covered:
                break
        if covered:
            done.add(_job_identity(job))
    return [job for job in jobs if _job_identity(job) not in done]


def offer_record(offer: Offer, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {
        "origin": offer.origin,
        "destination": offer.destination,
        "origin_city": city_name(offer.origin),
        "destination_city": city_name(offer.destination),
        "departure_date": offer.departure_date,
        "return_date": offer.return_date,
        "airline": offer.airline,
        "stops": offer.stops,
        "cabin": offer.cabin,
        "price_type": "miles",
        "currency": offer.currency,
        "milhas": offer.miles,
        "program": offer.miles_program,
        "source": offer.source,
        "booking_url": offer.booking_url,
        "departure_time": offer.departure_time,
        "arrival_time": offer.arrival_time,
        "return_time": offer.return_time,
        "return_arrival": offer.return_arrival,
        "duration": offer.duration,
        "operators": offer.operators,
        "layover": offer.layover,
        "taxes": offer.taxes,
        "fare": offer.fare,
        "trip_kind": offer.trip_kind,
    }
    if extra:
        record.update(extra)
    return record


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    miles = [item for item in records if item.get("milhas")]
    miles.sort(key=lambda item: (item["milhas"], item["origin"], item["destination"], item["departure_date"]))
    return miles


def take_run(jobs: list[dict[str, Any]], limit: int, keep_order: bool = False) -> list[tuple[str, list[dict[str, Any]]]]:
    if not jobs:
        return []
    if keep_order:
        label = jobs[0].get("region_label") or "pedido"
        return [(label, jobs[: max(1, limit)])]
    international = [job for job in jobs if job.get("kind") == "international"]
    national = [job for job in jobs if job.get("kind") != "international"]
    pool = international or national
    if not pool:
        return []
    label = "internacionais" if international else "nacionais"
    return [(label, pool[: max(1, limit)])]


def records_from_db(origin: str | None = None) -> list[dict[str, Any]]:
    meta = {item["iata"]: item for item in catalog_airports()}
    records: list[dict[str, Any]] = []
    rows = query_results(origin=origin, price_type="miles", sort="miles", limit=2000)
    for row in rows:
        program = (row.get("miles_program") or "").lower()
        source = (row.get("source") or "").lower()
        if program not in {"latam", "azul"} and source not in {
            "latam",
            "latampass",
            "azul",
            "tudoazul",
        }:
            continue
        dest = (row.get("destination") or "").upper()
        info = meta.get(dest) or {}
        records.append(
            {
                "origin": row.get("origin"),
                "destination": dest,
                "origin_city": city_name(row.get("origin") or ""),
                "destination_city": city_name(dest),
                "departure_date": row.get("departure_date"),
                "return_date": row.get("return_date"),
                "airline": row.get("airline"),
                "stops": row.get("stops"),
                "cabin": row.get("cabin"),
                "price_type": "miles",
                "currency": row.get("currency") or "PTS",
                "milhas": row.get("miles"),
                "program": row.get("miles_program") or "latam",
                "source": row.get("source"),
                "booking_url": row.get("booking_url"),
                "region": info.get("region_label"),
                "kind": info.get("kind"),
                "departure_time": row.get("departure_time"),
                "arrival_time": row.get("arrival_time"),
                "return_time": row.get("return_time"),
                "return_arrival": row.get("return_arrival"),
                "duration": row.get("duration"),
                "operators": row.get("operators"),
                "layover": row.get("layover"),
                "taxes": row.get("taxes"),
                "fare": row.get("fare"),
                "trip_kind": row.get("trip_kind"),
            }
        )
    return records


def seed_latest_from_db() -> dict[str, Any]:
    records = records_from_db()
    return {
        "ok": True,
        "milhas": len(sort_records(records)),
        "notes": ["Os cards leem o banco; sem pasta extra de snapshot."],
    }


async def run_snapshot(
    kinds: tuple[str, ...] = ("miles",),
    jobs: list[dict[str, Any]] | None = None,
    max_per_run: int | None = None,
    keep_order: bool = False,
    programs: list[str] | None = None,
) -> dict[str, Any]:
    init_db()
    catalog = load_catalog()
    planned = jobs if jobs is not None else snapshot_queue(catalog)
    if planned:
        planned = collapse_rio_jobs(planned)
    notes: list[str] = []
    records: list[dict[str, Any]] = records_from_db()
    if not planned:
        return {"ok": False, "error": "nenhum trecho neste pedido", "milhas": 0, "notes": []}
    origins = sorted({job["origin"] for job in planned})
    search_id = create_search(
        {
            "origins": origins,
            "destinations": sorted({job["destination"] for job in planned}),
            "trip_type": "one_way",
            "date_mode": "flex",
            "date_start": min(job["day"] for job in planned),
            "date_end": max(job["day"] for job in planned),
            "stay_nights": 7,
            "cabin": "economy",
            "include_cash": any(name == "smiles" for name in (programs or [])),
            "include_miles": True,
        }
    )
    found_at = now_iso()
    update_search(search_id, status="running", progress="Coleta do pedido")

    if "miles" in kinds:
        wanted = {name.lower() for name in (programs or ["latam"]) if name.lower() in {"latam", "azul", "smiles"}}
        if not wanted:
            wanted = {"latam"}
        airlines = [
            ("latam", "LATAM Pass", latam_session_ready, collect_latam_jobs),
            ("azul", "Azul", azul_session_ready, collect_azul_jobs),
            ("smiles", "GOL/Smiles", smiles_session_ready, collect_smiles_jobs),
        ]
        limit = max_per_run or int(catalog.get("max_per_run") or LATAM_MAX_PER_RUN)
        for program, title, ready, collect in airlines:
            if program not in wanted:
                continue
            if program == "latam" and not ready():
                notes.append(
                    "LATAM: se pedir login, entre você no Chrome. Uso só os cookies salvos; não preencho senha."
                )
            miles_dir = HARVEST_DIR / program
            pending = pending_jobs(planned, miles_dir)
            batches = take_run(pending, limit, keep_order=keep_order)
            print(
                f"{title}: {len(planned)} trechos no total, {len(pending)} ainda pendentes, {limit} nesta rodada.",
                flush=True,
            )
            for label, batch in batches:
                batch = [{**job, "program": program} for job in batch]
                gap_note = (
                    "20-60s aleatórios na tela de resultados entre cada um"
                    if program in {"azul", "smiles"}
                    else "2-3 min na tela de resultados entre cada um"
                )
                print(f"Leva {title} {label}: {len(batch)} trechos; {gap_note}.", flush=True)
                update_search(search_id, progress=f"{title} milhas {label} ({len(batch)} trechos)")

                def ingest(item: dict[str, Any], current_program: str = program, current_title: str = title) -> None:
                    job = item["job"]
                    offers = item.get("offers") or []
                    if item.get("status") == "login":
                        notes.append(f"{current_title} pediu login de novo")
                    status = item.get("status")
                    offers = [
                        offer
                        for offer in (item.get("offers") or [])
                        if (
                            smiles_keep_offer(
                                offer.miles,
                                offer.price_cash,
                                offer.origin,
                                offer.destination,
                            )
                            if (job.get("program") or offer.miles_program or "") == "smiles"
                            else plausible_miles(
                                offer.miles,
                                offer.origin,
                                offer.destination,
                                offer.cabin,
                                program=job.get("program") or offer.miles_program or "latam",
                                fare=offer.fare,
                                trip_kind=offer.trip_kind,
                            )
                        )
                    ]
                    complete = bool(offers) and status in {200, "ok"}
                    if job.get("return_day") and complete:
                        complete = any((offer.trip_kind or "") in {"volta", "round_trip"} for offer in offers)
                    if not complete:
                        print(
                            f"Não atualizo o site {job['origin']}-{job['destination']}; mantenho a coleta anterior.",
                            flush=True,
                        )
                        return
                    if offers:
                        origin_codes = expand_city_airports(job["origin"])
                        dests = expand_city_airports(job["destination"])
                        for origin_code in origin_codes:
                            for dest_code in dests:
                                if origin_code != dest_code:
                                    for cabin in {offer.cabin for offer in offers}:
                                        replace_miles_route(
                                            origin_code,
                                            dest_code,
                                            job["day"],
                                            current_program,
                                            cabin=cabin,
                                            return_date=job.get("return_day") or job.get("return_date"),
                                        )
                        insert_results([offer.as_row(search_id, found_at) for offer in offers])
                        for offer in offers:
                            records.append(offer_record(offer, {"region": job.get("region_label"), "kind": job.get("kind")}))
                        print(
                            f"Salvo {current_title} {job['origin']}-{job['destination']} {job['day']}: "
                            + ", ".join(
                                f"{kind} {sum(1 for item in offers if (item.trip_kind or 'round_trip') == kind)}"
                                for kind in ("ida", "volta", "round_trip")
                                if any((item.trip_kind or "round_trip") == kind for item in offers)
                            )
                            or f"{len(offers)} ofertas",
                            flush=True,
                        )

                extra = {}
                if program == "latam":
                    extra = {"redemption": True, "wait_loops": 30}
                collected = await collect(
                    batch,
                    miles_dir,
                    pause=None,
                    halt_on={"login", "denied"} if program != "azul" else {"denied"},
                    save_raw=True,
                    on_result=ingest,
                    **extra,
                )
                statuses = {str(item.get("status")) for item in collected or []}
                if statuses & {"throttled", "denied"}:
                    notes.append(f"{title} limitou o acesso; paro as próximas levas.")
                    break
                del collected

    miles = sort_records(records)
    progress = f"Catálogo: {len(miles)} milhas"
    if notes:
        progress += " · " + "; ".join(notes)
    update_search(search_id, status="done", progress=progress, error=None)
    return {
        "ok": True,
        "search_id": search_id,
        "milhas": len(miles),
        "notes": notes,
    }


def summarize_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    lista = [
        (
            f"{job['origin']}->{job['destination']} {job['day']}"
            + (f" / volta {job.get('return_day')}" if job.get("return_day") else f" ({job.get('leg') or 'ida'})")
        )
        for job in jobs
    ]
    paired = [job for job in jobs if job.get("return_day")]
    payload: dict[str, Any] = {
        "trechos": len(jobs),
        "destinos": sorted({job["destination"] if job.get("leg") != "volta" else job["origin"] for job in jobs}),
        "datas": sorted({job["day"] for job in jobs} | {str(job.get("return_day")) for job in paired}),
        "idas": sum(1 for job in jobs if job.get("leg") != "volta"),
        "voltas": sum(1 for job in jobs if job.get("leg") == "volta" or job.get("return_day")),
        "buscas_ida_volta": len(paired),
    }
    if len(lista) <= 40:
        payload["lista"] = lista
    else:
        payload["amostra"] = lista[:6] + ["…"] + lista[-4:]
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Coleta LATAM, Azul e GOL/Smiles só do que você pedir. Sem pedido, não busca o catálogo inteiro.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("from-db", help="Contar milhas que já estão no banco, sem buscar nas cias")
    sub.add_parser("catalog", help="Varredura antiga do catálogo (evitar; gasta muitas buscas)")
    ask = sub.add_parser("ask", help="Pedido pontual: origem, destino/região e datas")
    ask.add_argument("--origem", "--origin", dest="origin", default="GIG")
    ask.add_argument("--para", "--where", dest="where", help="Região, cidade ou IATA, ex.: nordeste, FCO, Roma")
    ask.add_argument("--destinos", dest="destinations", help="IATAs separados por vírgula")
    ask.add_argument("--mes", "--month", dest="month", help="AAAA-MM, amostra dia 10 e 20")
    ask.add_argument("--datas", "--dates", dest="dates", help="Datas AAAA-MM-DD separadas por vírgula")
    ask.add_argument("--ida-volta", dest="invert", action="store_true", default=True)
    ask.add_argument("--so-ida", dest="invert", action="store_false")
    ask.add_argument(
        "--finais-de-semana",
        dest="weekends",
        action="store_true",
        help="Ida sexta e volta segunda de cada fim de semana do --mes",
    )
    ask.add_argument("--dry-run", action="store_true", help="Só mostra os trechos, não abre o Chrome")
    ask.add_argument("--limite", type=int, default=None, help="Máximo nesta rodada (padrão 6)")
    ask.add_argument("--de", dest="date_from", help="Primeira data de ida AAAA-MM-DD")
    ask.add_argument("--ate", dest="date_to", help="Última data de ida AAAA-MM-DD (máx. 90 dias)")
    ask.add_argument("--volta-de", dest="return_from", help="Primeira data de volta/ida inversa AAAA-MM-DD")
    ask.add_argument("--volta-ate", dest="return_to", help="Última data de volta/ida inversa AAAA-MM-DD")
    ask.add_argument("--programas", dest="programs", help="latam, azul, smiles ou vários separados por vírgula")
    ask.add_argument(
        "--continuar",
        action="store_true",
        help="Segue levas de 6 trechos até acabar ou a cia limitar",
    )
    ask.add_argument(
        "--azul",
        action="store_true",
        help="Inclui TudoAzul nesta rodada; o padrão é só LATAM",
    )
    args = parser.parse_args(argv)

    if args.command == "from-db":
        print(seed_latest_from_db())
        return 0
    if args.command == "catalog":
        result = asyncio.run(run_snapshot(("miles",), programs=["latam"]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "ask":
        dests = [item.strip() for item in (args.destinations or "").split(",") if item.strip()]
        dates = [item.strip() for item in (args.dates or "").split(",") if item.strip()]
        origins = [item.strip().upper() for item in (args.origin or "GIG").split(",") if item.strip()]
        programs = [item.strip().lower() for item in (args.programs or "").split(",") if item.strip()]
        if not programs:
            programs = ["latam", "azul"] if args.azul else ["latam"]
        jobs: list[dict[str, Any]] = []
        for origin in origins:
            for program in programs:
                jobs.extend(
                    requested_jobs(
                        origin=normalize_city_token(origin) or origin,
                        where=args.where,
                        destinations=dests or None,
                        month=args.month,
                        dates=dates or None,
                        invert=args.invert,
                        weekends=args.weekends,
                        program=program,
                        date_from=args.date_from,
                        date_to=args.date_to,
                        return_from=args.return_from,
                        return_to=args.return_to,
                    )
                )
        jobs = collapse_rio_jobs(jobs)
        summary = summarize_jobs(jobs)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.dry_run or not jobs:
            return 0 if jobs else 1
        while True:
            pending = []
            for program in programs:
                pending.extend(pending_jobs(jobs, HARVEST_DIR / program))
            if args.continuar:
                print(f"Ainda faltam {len(pending)} trechos nesta varredura.", flush=True)
            result = asyncio.run(
                run_snapshot(
                    ("miles",),
                    jobs=jobs,
                    max_per_run=args.limite,
                    keep_order=True,
                    programs=programs,
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not args.continuar:
                return 0 if result.get("ok") else 1
            notes = " ".join(result.get("notes") or [])
            leftover = []
            for program in programs:
                leftover.extend(pending_jobs(jobs, HARVEST_DIR / program))
            if "limitou" in notes or not leftover or len(leftover) >= len(pending):
                return 0 if result.get("ok") else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
