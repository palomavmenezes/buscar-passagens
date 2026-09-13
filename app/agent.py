from __future__ import annotations

import traceback
from typing import Any

from app.dates import parse_date, resolve_window
from app.db import insert_results, now_iso, update_search
from app.harvest import offers_from_cache


async def run_search(search_id: int, payload: dict[str, Any]) -> None:
    try:
        update_search(search_id, status="running", progress="Preparando janela de datas")
        start, end = resolve_window(
            payload["date_mode"],
            payload.get("date_start"),
            payload.get("date_end"),
        )
        if payload["date_mode"] == "specific" and payload["trip_type"] == "round_trip":
            ida = parse_date(payload.get("date_start"))
            volta = parse_date(payload.get("date_end"))
            if ida:
                start = end = ida
            if ida and volta and volta > ida:
                pass
        origins = payload["origins"]
        destinations = payload["destinations"]
        programs = payload.get("programs") or ["azul", "latam"]

        found_at = now_iso()
        rows: list[dict[str, Any]] = []
        notes: list[str] = []

        update_search(search_id, progress="Lendo milhas da coleta salva")
        cached = offers_from_cache(origins, destinations, start, end, programs)
        for row in cached:
            copied = dict(row)
            copied.pop("id", None)
            copied["search_id"] = search_id
            copied["found_at"] = found_at
            rows.append(copied)
        if cached:
            notes.append(f"{len(cached)} milhas do cache")
        else:
            notes.append("sem milhas na coleta ainda")

        insert_results(rows)
        update_search(
            search_id,
            status="done",
            progress=f"Concluído: {len(rows)} ofertas ({', '.join(notes)})",
            error=None,
        )
    except Exception as exc:
        update_search(
            search_id,
            status="error",
            progress="Falhou",
            error=f"{exc}\n{traceback.format_exc(limit=4)}",
        )
