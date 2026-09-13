from __future__ import annotations

from typing import Any

from app.airports import decorate, related_airports
from app.dates import duration_minutes, format_updated
from app.db import query_results
from app.miles_budget import EXCELLENT_MILES, miles_within_budget


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


def _duration_key(row: dict[str, Any]) -> int:
    minutes = duration_minutes(row.get("duration"))
    return minutes if minutes is not None else 10**9


def _better_deal(candidate: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if current is None:
        return True
    left = int(candidate["miles"])
    right = int(current["miles"])
    if left < right:
        return True
    if left > right:
        return False
    return _duration_key(candidate) < _duration_key(current)


def tag_deal_flags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        miles = row.get("miles")
        row["excellent_price"] = bool(miles) and int(miles) < EXCELLENT_MILES
        row["best_price"] = False
        kind = (row.get("trip_kind") or "ida").lower()
        if kind not in {"ida", "volta", "round_trip"}:
            kind = "round_trip" if row.get("return_date") else "ida"
        key = (kind, row.get("cabin") or "economy")
        groups.setdefault(key, []).append(row)
    for items in groups.values():
        priced = [row for row in items if row.get("miles")]
        if not priced:
            continue
        winner = min(priced, key=lambda row: (int(row["miles"]), _duration_key(row)))
        best_miles = int(winner["miles"])
        best_duration = _duration_key(winner)
        for row in priced:
            if int(row["miles"]) == best_miles and _duration_key(row) == best_duration:
                row["best_price"] = True
    return rows


def _best_by_key(rows: list[dict[str, Any]], key_of) -> dict[Any, dict[str, Any]]:
    best: dict[Any, dict[str, Any]] = {}
    for row in rows:
        miles = row.get("miles")
        if not miles:
            continue
        key = key_of(row)
        current = best.get(key)
        if _better_deal(row, current):
            best[key] = row
        elif current is not None and int(miles) == int(current["miles"]) and _duration_key(row) == _duration_key(current):
            if str(row.get("found_at") or "") > str(current.get("found_at") or ""):
                best[key] = row
    return best


def _with_meta(row: dict[str, Any]) -> dict[str, Any]:
    item = decorate(row)
    item["updated"] = format_updated(row.get("found_at"))
    miles = item.get("miles")
    item["excellent_price"] = bool(miles) and int(miles) < EXCELLENT_MILES
    item["best_price"] = False
    return item


def _mark_best(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return tag_deal_flags(items)


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


def _card_in_budget(
    card: dict[str, Any],
    origin: str,
    trip_type: str,
    miles_min: int | None,
    miles_max: int | None,
) -> bool:
    dest = str(card.get("destination") or "")
    cabin = str(card.get("cabin") or "economy")
    program = str(card.get("miles_program") or "latam")
    fare = card.get("fare")
    return miles_within_budget(
        card.get("miles"),
        origin or str(card.get("origin") or ""),
        dest,
        program=program,
        cabin=cabin,
        fare=str(fare) if fare else None,
        trip_type=trip_type,
        miles_min=miles_min,
        miles_max=miles_max,
    )


def apply_miles_budget(
    cards: dict[str, list[dict[str, Any]]],
    origin: str,
    trip_type: str,
    miles_min: int | None,
    miles_max: int | None,
) -> dict[str, list[dict[str, Any]]]:
    def keep(card: dict[str, Any]) -> bool:
        return _card_in_budget(card, origin, trip_type, miles_min, miles_max)

    national = _mark_best([card for card in cards.get("national") or [] if keep(card)])
    international = _mark_best([card for card in cards.get("international") or [] if keep(card)])
    return {"national": national, "international": international, "all": national + international}


def filter_rows_budget(
    rows: list[dict[str, Any]],
    origin: str,
    trip_type: str,
    miles_min: int | None,
    miles_max: int | None,
) -> list[dict[str, Any]]:
    return [row for row in rows if _card_in_budget(row, origin, trip_type, miles_min, miles_max)]


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
