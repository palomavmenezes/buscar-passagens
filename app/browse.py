from __future__ import annotations

from typing import Any

from app.airports import decorate, related_airports
from app.dates import format_updated
from app.db import query_results


def _filters(
    cabin: str | None = None,
    program: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    airline: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"price_type": "miles", "sort": "miles", "limit": 4000}
    if cabin and cabin != "all":
        payload["cabin"] = cabin
    if program:
        payload["miles_program"] = program
    if date_from:
        payload["date_from"] = date_from
    if date_to:
        payload["date_to"] = date_to
    if airline:
        payload["airline"] = airline
    return payload


def _round_trip_filters(**filters: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    shared = {
        "cabin": filters.get("cabin"),
        "program": filters.get("program"),
        "airline": filters.get("airline"),
    }
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    outbound = {**shared, "date_from": date_from, "date_to": date_from or None}
    inbound = {**shared, "date_from": date_to, "date_to": date_to or None}
    return outbound, inbound


def _is_one_way(row: dict[str, Any]) -> bool:
    kind = (row.get("trip_kind") or "ida").lower()
    return kind in {"ida", "volta"}


def _best_by_key(rows: list[dict[str, Any]], key_of) -> dict[Any, dict[str, Any]]:
    best: dict[Any, dict[str, Any]] = {}
    for row in rows:
        miles = row.get("miles")
        if not miles:
            continue
        key = key_of(row)
        current = best.get(key)
        if current is None or int(miles) < int(current["miles"]):
            best[key] = row
        elif int(miles) == int(current["miles"]):
            if str(row.get("found_at") or "") > str(current.get("found_at") or ""):
                best[key] = row
    return best


def _with_meta(row: dict[str, Any]) -> dict[str, Any]:
    item = decorate(row)
    item["updated"] = format_updated(row.get("found_at"))
    item["best_price"] = False
    return item


def _mark_best(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priced = [item for item in items if item.get("miles")]
    if not priced:
        return items
    best = min(int(item["miles"]) for item in priced)
    for item in items:
        item["best_price"] = bool(item.get("miles")) and int(item["miles"]) == best
    return items


def cheapest_one_ways(
    origin: str,
    destination: str | None = None,
    **filters: Any,
) -> list[dict[str, Any]]:
    rows = query_results(
        origin=origin,
        destination=destination or None,
        trip_kind="one_way",
        **_filters(**filters),
    )
    home = set(related_airports(origin))
    outbound = [
        row
        for row in rows
        if _is_one_way(row) and (row.get("origin") or "").upper() in home
        and (row.get("destination") or "").upper() not in home
    ]
    best = _best_by_key(outbound, lambda row: row.get("destination"))
    cards = [_with_meta(row) for row in best.values()]
    cards.sort(key=lambda item: (item.get("miles") or 10**12, item.get("city") or ""))
    national = _mark_best([card for card in cards if card.get("national")])
    international = _mark_best([card for card in cards if not card.get("national")])
    return {"national": national, "international": international, "all": national + international}


def cheapest_round_trips(
    origin: str,
    destination: str | None = None,
    **filters: Any,
) -> list[dict[str, Any]]:
    out_filters, in_filters = _round_trip_filters(**filters)
    out_rows = query_results(
        origin=origin,
        destination=destination or None,
        trip_kind="one_way",
        **_filters(**out_filters),
    )
    in_rows = query_results(
        origin=destination or None,
        destination=origin,
        trip_kind="one_way",
        **_filters(**in_filters),
    )
    home = set(related_airports(origin))
    outbound = [
        row
        for row in out_rows
        if _is_one_way(row)
        and (row.get("origin") or "").upper() in home
        and (row.get("destination") or "").upper() not in home
    ]
    inbound = [
        row
        for row in in_rows
        if _is_one_way(row)
        and (row.get("destination") or "").upper() in home
        and (row.get("origin") or "").upper() not in home
    ]
    best_out = _best_by_key(outbound, lambda row: row.get("destination"))
    best_in = _best_by_key(inbound, lambda row: row.get("origin"))
    cards: list[dict[str, Any]] = []
    for dest, going in best_out.items():
        back = best_in.get(dest)
        if not back:
            continue
        going_card = _with_meta(going)
        back_card = _with_meta(back)
        found = max(str(going.get("found_at") or ""), str(back.get("found_at") or ""))
        oldest = min(
            str(going.get("found_at") or found),
            str(back.get("found_at") or found),
        )
        cards.append(
            {
                **going_card,
                "outbound": going_card,
                "inbound": back_card,
                "miles": int(going["miles"]) + int(back["miles"]),
                "found_at": found,
                "updated": format_updated(found),
                "updated_oldest": format_updated(oldest),
                "miles_return": back.get("departure_date"),
                "trip_kind": "round_trip",
            }
        )
    cards.sort(key=lambda item: item.get("miles") or 10**12)
    national = _mark_best([card for card in cards if card.get("national")])
    international = _mark_best([card for card in cards if not card.get("national")])
    return {"national": national, "international": international, "all": national + international}


def browse_flights(
    origin: str | None,
    destination: str | None,
    trip_type: str,
    **filters: Any,
) -> list[dict[str, Any]]:
    trip_kind = "one_way" if trip_type != "round_trip" else "round_trip"
    rows = query_results(
        origin=origin or None,
        destination=destination or None,
        trip_kind=trip_kind,
        **_filters(**filters),
    )
    return [_with_meta(row) for row in rows]
