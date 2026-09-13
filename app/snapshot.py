from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.airports import city_name, expand_city_airports
from app.catalog import catalog_airports, collapse_rio_jobs, load_catalog, requested_jobs, snapshot_queue
from app.collectors.azul import azul_session_ready, collect_azul_jobs
from app.collectors.latam import collect_latam_jobs, latam_session_ready, plausible_miles
from app.collectors.smiles import collect_smiles_jobs, smiles_session_ready
from app.config import HARVEST_DIR, LATAM_MAX_PER_RUN, harvest_route_path
from app.db import create_search, init_db, insert_results, now_iso, query_results, replace_miles_route, update_search
from app.providers.base import Offer


def pending_jobs(jobs: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
    done: set[tuple[str, str, str, str]] = set()
    for job in jobs:
        path = harvest_route_path(folder, job["origin"], job["destination"], job["day"], job=job)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = payload.get("status")
        offers = payload.get("offers") or []
        saved = payload.get("job") or {}
        key = (
            saved.get("origin") or job["origin"],
            saved.get("destination") or job["destination"],
            saved.get("day") or job["day"],
            saved.get("return_day") or saved.get("return_date") or job.get("return_day") or job.get("return_date") or "",
        )
        if status in {200, "ok"} and offers:
            done.add(key)
        elif status == "empty":
            done.add(key)
    return [
        job
        for job in jobs
        if (job["origin"], job["destination"], job["day"], job.get("return_day") or job.get("return_date") or "") not in done
    ]


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
            "include_cash": False,
            "include_miles": True,
        }
    )
    found_at = now_iso()
    update_search(search_id, status="running", progress="Coleta do pedido")

    if "miles" in kinds:
        airlines = [
            ("latam", "LATAM Pass", latam_session_ready, collect_latam_jobs, "python3 -m app.collectors.latam login"),
            ("azul", "Azul", azul_session_ready, collect_azul_jobs, "python3 -m app.collectors.azul login"),
            ("smiles", "GOL/Smiles", smiles_session_ready, collect_smiles_jobs, "python3 -m app.collectors.smiles login"),
        ]
        limit = max_per_run or int(catalog.get("max_per_run") or LATAM_MAX_PER_RUN)
        for program, title, ready, collect, login_cmd in airlines:
            if not ready():
                notes.append(f"{title}: faça login com {login_cmd}")
                continue
            miles_dir = HARVEST_DIR / program
            pending = pending_jobs(planned, miles_dir)
            batches = take_run(pending, limit, keep_order=keep_order)
            print(
                f"{title}: {len(planned)} trechos no total, {len(pending)} ainda pendentes, {limit} nesta rodada.",
                flush=True,
            )
            for label, batch in batches:
                batch = [{**job, "program": program} for job in batch]
                print(f"Leva {title} {label}: {len(batch)} trechos; 2-3 min na tela de resultados entre cada um.", flush=True)
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
                    halt_on={"login", "denied"},
                    save_raw=True,
                    on_result=ingest,
                    **extra,
                )
                if notes and notes[-1].endswith("pediu login de novo"):
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
    return {
        "trechos": len(jobs),
        "destinos": sorted({job["destination"] if job.get("leg") != "volta" else job["origin"] for job in jobs}),
        "datas": sorted({job["day"] for job in jobs}),
        "idas": sum(1 for job in jobs if job.get("leg") != "volta"),
        "voltas": sum(1 for job in jobs if job.get("leg") == "volta"),
        "lista": [f"{job['origin']}->{job['destination']} {job['day']} ({job.get('leg') or 'ida'})" for job in jobs],
    }


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
    args = parser.parse_args(argv)

    if args.command == "from-db":
        print(seed_latest_from_db())
        return 0
    if args.command == "catalog":
        result = asyncio.run(run_snapshot(("miles",)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "ask":
        dests = [item.strip() for item in (args.destinations or "").split(",") if item.strip()]
        dates = [item.strip() for item in (args.dates or "").split(",") if item.strip()]
        origins = [item.strip().upper() for item in (args.origin or "GIG").split(",") if item.strip()]
        jobs: list[dict[str, Any]] = []
        for origin in origins:
            jobs.extend(
                requested_jobs(
                    origin="RIO" if origin in {"RJ", "RIO DE JANEIRO"} else origin,
                    where=args.where,
                    destinations=dests or None,
                    month=args.month,
                    dates=dates or None,
                    invert=args.invert,
                    weekends=args.weekends,
                )
            )
        jobs = collapse_rio_jobs(jobs)
        summary = summarize_jobs(jobs)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.dry_run or not jobs:
            return 0 if jobs else 1
        result = asyncio.run(
            run_snapshot(
                ("miles",),
                jobs=jobs,
                max_per_run=args.limite,
                keep_order=True,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
