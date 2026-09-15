from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.airports import CITY_AIRPORTS, city_group, city_name, related_airports
from app.config import HARVEST_DIR
from app.dates import format_br_date, parse_date
from app.db import get_db, now_iso, query_results
from app.miles_budget import max_miles_per_leg, min_miles_per_leg, plausible_miles

CHART_SPANS: dict[str, tuple[str, int]] = {
    "semana": ("1 semana", 7),
    "mes": ("1 mês", 30),
    "3meses": ("3 meses", 90),
    "6meses": ("6 meses", 180),
    "ano": ("1 ano", 365),
}


def _parse_when(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        day = parse_date(text[:10])
        if not day:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snapshot_key(row: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    origin = str(row.get("origin") or "").upper()
    dest = str(row.get("destination") or "").upper()
    day = str(row.get("departure_date") or "")[:10]
    program = str(row.get("miles_program") or row.get("program") or "").lower()
    kind = str(row.get("trip_kind") or "ida").lower()
    if kind not in {"ida", "volta", "round_trip"}:
        kind = "round_trip" if row.get("return_date") else "ida"
    if not origin or not dest or len(day) != 10 or not program:
        return None
    return origin, dest, day, program, kind


def record_cheapest_from_rows(rows: list[dict[str, Any]], collected_at: str | None = None) -> int:
    buckets: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    stamp = collected_at or now_iso()
    for row in rows:
        miles = row.get("miles")
        if miles is None:
            continue
        if not plausible_miles(
            miles,
            str(row.get("origin") or ""),
            str(row.get("destination") or ""),
            str(row.get("cabin") or "economy"),
            program=str(row.get("miles_program") or row.get("program") or "latam"),
            fare=row.get("fare"),
            trip_kind=row.get("trip_kind"),
        ):
            continue
        key = _snapshot_key(row)
        if not key:
            continue
        value = int(miles)
        current = buckets.get(key)
        found = str(row.get("found_at") or stamp)
        if current is None or value < int(current["miles"]):
            buckets[key] = {
                "origin": key[0],
                "destination": key[1],
                "departure_date": key[2],
                "miles_program": key[3],
                "trip_kind": key[4],
                "miles": value,
                "collected_at": found,
            }
    if not buckets:
        return 0
    saved = 0
    with get_db() as db:
        for item in buckets.values():
            last = db.execute(
                """
                SELECT id, miles, collected_at FROM price_snapshots
                WHERE origin = ? AND destination = ? AND departure_date = ?
                  AND miles_program = ? AND trip_kind = ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (
                    item["origin"],
                    item["destination"],
                    item["departure_date"],
                    item["miles_program"],
                    item["trip_kind"],
                ),
            ).fetchone()
            last_day = str(last["collected_at"] or "")[:10] if last else ""
            new_day = str(item["collected_at"] or "")[:10]
            if last and last_day == new_day:
                if int(last["miles"]) == int(item["miles"]):
                    continue
                db.execute(
                    """
                    UPDATE price_snapshots
                    SET miles = ?, collected_at = ?
                    WHERE id = ?
                    """,
                    (item["miles"], item["collected_at"], last["id"]),
                )
                saved += 1
                continue
            db.execute(
                """
                INSERT INTO price_snapshots (
                    origin, destination, departure_date, miles_program,
                    trip_kind, miles, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["origin"],
                    item["destination"],
                    item["departure_date"],
                    item["miles_program"],
                    item["trip_kind"],
                    item["miles"],
                    item["collected_at"],
                ),
            )
            saved += 1
    return saved


def list_snapshots(
    origin: str | None = None,
    destination: str | None = None,
    departure_date: str | None = None,
    miles_program: str | None = None,
    trip_kind: str | None = None,
    limit: int = 20000,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    origin_codes = related_airports(origin) if origin else []
    dest_codes = related_airports(destination) if destination else []
    if origin_codes:
        placeholders = ", ".join("?" for _ in origin_codes)
        clauses.append(f"origin IN ({placeholders})")
        params.extend(origin_codes)
    if dest_codes:
        placeholders = ", ".join("?" for _ in dest_codes)
        clauses.append(f"destination IN ({placeholders})")
        params.extend(dest_codes)
    if departure_date:
        clauses.append("departure_date = ?")
        params.append(departure_date[:10])
    if miles_program:
        clauses.append("miles_program = ?")
        params.append(miles_program.lower())
    if trip_kind:
        clauses.append("trip_kind = ?")
        params.append(trip_kind)
    sql = f"""
        SELECT origin, destination, departure_date, miles_program, trip_kind, miles, collected_at
        FROM price_snapshots
        WHERE {' AND '.join(clauses)}
        ORDER BY collected_at ASC, id ASC
        LIMIT ?
    """
    params.append(limit)
    with get_db() as db:
        rows = [dict(row) for row in db.execute(sql, params).fetchall()]
    return [row for row in rows if _snapshot_plausible(row)]


def _snapshot_plausible(row: dict[str, Any]) -> bool:
    origin = str(row.get("origin") or "")
    dest = str(row.get("destination") or "")
    miles = row.get("miles")
    program = str(row.get("miles_program") or "latam")
    if not miles:
        return False
    if int(miles) < min_miles_per_leg(origin, dest, "economy"):
        return False
    cap = max_miles_per_leg(origin, dest, program=program, cabin="business", trip_kind=row.get("trip_kind"))
    return cap is None or int(miles) <= cap


def _series_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str, str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _snapshot_key(row)
        if not key:
            continue
        index.setdefault(key, []).append(row)
    for series in index.values():
        series.sort(key=lambda row: (str(row.get("collected_at") or ""), int(row.get("id") or 0)))
    return index


def _as_day(value: str | date | None) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "")[:10]
    try:
        return parse_date(text)
    except ValueError:
        return None


def _move(was: int, now: int) -> dict[str, Any] | None:
    if was == now:
        return None
    return {
        "fell": now < was,
        "rose": now > was,
        "was": was,
        "now": now,
        "delta": now - was,
        "changed": True,
    }


def _best_by_day(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row.get("collected_at") or "")[:10]
        miles = row.get("miles")
        if not day or miles is None:
            continue
        current = best.get(day)
        if current is None or int(miles) < int(current["miles"]):
            best[day] = row
    return best


def _annotate_move(
    move: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not move:
        return None
    if previous:
        move["was_collected"] = str(previous.get("collected_at") or "")[:10]
        move["was_flight"] = str(previous.get("departure_date") or "")[:10]
        move["was_origin"] = previous.get("origin")
        move["first_at"] = previous.get("collected_at")
    if current:
        move["now_collected"] = str(current.get("collected_at") or "")[:10]
        move["now_flight"] = str(current.get("departure_date") or "")[:10]
        move["now_origin"] = current.get("origin")
    return move


def price_move_for(
    row: dict[str, Any],
    index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] | None = None,
    travel_index: dict[str, int] | None = None,
    require_change: bool = True,
) -> dict[str, Any] | None:
    miles = row.get("miles")
    if miles is None:
        return None
    key = _snapshot_key(row)
    if not key:
        return None
    series = (index or {}).get(key) or []
    current = int(miles)
    if len(series) < 2:
        return None if require_change else {
            "fell": False,
            "rose": False,
            "was": current,
            "now": current,
            "delta": 0,
            "changed": False,
        }
    previous = series[-2]
    move = _move(int(previous["miles"]), current)
    if not move:
        return None if require_change else {
            "fell": False,
            "rose": False,
            "was": int(previous["miles"]),
            "now": current,
            "delta": 0,
            "changed": False,
        }
    return _annotate_move(move, previous, series[-1])


def _move_from_best_days(best: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    seq = sorted(best.items())
    if len(seq) < 2:
        return None
    previous = seq[-2][1]
    current = seq[-1][1]
    return _annotate_move(_move(int(previous["miles"]), int(current["miles"])), previous, current)


def _leg_rows(
    snapshots: list[dict[str, Any]],
    origin: str,
    destination: str,
    *,
    program: str | None = None,
) -> list[dict[str, Any]]:
    origin_code = str(origin or "").upper()
    dest_code = str(destination or "").upper()
    wanted_program = str(program or "").lower()
    return [
        row
        for row in snapshots
        if str(row.get("origin") or "").upper() == origin_code
        and str(row.get("destination") or "").upper() == dest_code
        and (not wanted_program or str(row.get("miles_program") or "").lower() == wanted_program)
    ]


def _rows_on_flight(
    snapshots: list[dict[str, Any]],
    origin: str,
    destination: str,
    flight: str,
    *,
    program: str | None = None,
) -> list[dict[str, Any]]:
    day = str(flight or "")[:10]
    return [
        row
        for row in _leg_rows(snapshots, origin, destination, program=program)
        if str(row.get("departure_date") or "")[:10] == day
    ]


def _by_flight_days(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        flight = str(row.get("departure_date") or "")[:10]
        day = str(row.get("collected_at") or "")[:10]
        miles = row.get("miles")
        if not flight or not day or miles is None:
            continue
        bucket = index.setdefault(flight, {})
        current = bucket.get(day)
        if current is None or int(miles) < int(current["miles"]):
            bucket[day] = row
    return index


def _days_from_series(series: list[dict[str, Any]]) -> dict[str, int]:
    best: dict[str, int] = {}
    for row in series:
        day = str(row.get("collected_at") or "")[:10]
        miles = row.get("miles")
        if not day or miles is None:
            continue
        value = int(miles)
        if day not in best or value < best[day]:
            best[day] = value
    return best


def _combine_moves(
    outbound: dict[str, Any],
    inbound: dict[str, Any],
    index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]],
    snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    out_miles = outbound.get("miles")
    in_miles = inbound.get("miles")
    if out_miles is None or in_miles is None:
        return None
    out_key = _snapshot_key(outbound)
    in_key = _snapshot_key(inbound)
    if not out_key or not in_key:
        return None
    out_days = _days_from_series(index.get(out_key) or [])
    in_days = _days_from_series(index.get(in_key) or [])
    if snapshots and (not out_days or not in_days):
        out_days = {
            day: int(row["miles"])
            for day, row in _best_by_day(
                _rows_on_flight(
                    snapshots,
                    str(outbound.get("origin") or ""),
                    str(outbound.get("destination") or ""),
                    str(outbound.get("departure_date") or ""),
                    program=str(outbound.get("miles_program") or ""),
                )
            ).items()
        }
        in_days = {
            day: int(row["miles"])
            for day, row in _best_by_day(
                _rows_on_flight(
                    snapshots,
                    str(inbound.get("origin") or ""),
                    str(inbound.get("destination") or ""),
                    str(inbound.get("departure_date") or ""),
                    program=str(inbound.get("miles_program") or ""),
                )
            ).items()
        }
    common = sorted(set(out_days) & set(in_days))
    if len(common) < 2:
        return None
    prev_day, last_day = common[-2], common[-1]
    was = int(out_days[prev_day]) + int(in_days[prev_day])
    now = int(out_miles) + int(in_miles)
    move = _move(was, now)
    if not move:
        return None
    out_date = str(outbound.get("departure_date") or "")[:10]
    in_date = str(inbound.get("departure_date") or "")[:10]
    move["was_collected"] = prev_day
    move["now_collected"] = last_day
    move["was_flight"] = out_date
    move["now_flight"] = out_date
    move["was_return"] = in_date
    move["now_return"] = in_date
    return move


def _snapshot_origin_query(code: str) -> str:
    token = str(code or "").upper()
    for city, members in CITY_AIRPORTS.items():
        if token == city or token in members:
            return city
    return token


def attach_price_moves(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    origins = {str(item.get("origin") or "") for item in items}
    origins.update(str(item.get("outbound", {}).get("origin") or "") for item in items)
    origins.update(str(item.get("inbound", {}).get("origin") or "") for item in items)
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for origin in origins:
        query = _snapshot_origin_query(origin)
        if not query or query in seen:
            continue
        seen.add(query)
        snapshots.extend(list_snapshots(origin=query))
    index = _series_index(snapshots)
    for item in items:
        outbound = item.get("outbound")
        inbound = item.get("inbound")
        if outbound and inbound:
            outbound["price_move"] = price_move_for(outbound, index)
            inbound["price_move"] = price_move_for(inbound, index)
            item["price_move"] = _combine_moves(outbound, inbound, index, snapshots)
        else:
            item["price_move"] = price_move_for(item, index)
    return items


def attach_card_moves(cards: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    for key in ("national", "international", "all"):
        attach_price_moves(cards.get(key) or [])
    return cards


def _travel_index(rows: list[dict[str, Any]]) -> dict[str, int]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row.get("departure_date") or "")[:10]
        if not day:
            continue
        current = latest.get(day)
        collected = str(row.get("collected_at") or "")
        if current is None or collected >= str(current.get("collected_at") or ""):
            latest[day] = row
    return {day: int(row["miles"]) for day, row in latest.items()}


def _collection_index(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_day: dict[str, int] = {}
    for row in rows:
        day = str(row.get("collected_at") or "")[:10]
        miles = int(row["miles"])
        if day and (day not in by_day or miles < by_day[day]):
            by_day[day] = miles
    return by_day


def _in_window(day: date | None, start: date, end: date) -> bool:
    return bool(day and start <= day <= end)


def chart_timed(
    series: list[dict[str, Any]],
    start: date,
    end: date,
    width: int = 960,
    height: int = 320,
) -> dict[str, Any]:
    pad_left, pad_right, pad_top, pad_bottom = 58, 52, 18, 42
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    span_days = max(1, (end - start).days)
    values = [point[1] for item in series for point in item.get("points") or []]
    low = min(values) if values else 0
    high = max(values) if values else 1
    span = max(1, high - low)

    def x_of(day: date) -> float:
        return pad_left + inner_w * ((day - start).days / span_days)

    def y_of(miles: int) -> float:
        return pad_top + inner_h * (1 - (miles - low) / span)

    drawn = []
    for item in series:
        coords = []
        for point in item.get("points") or []:
            raw = point[0]
            miles = point[1]
            extra = point[2] if len(point) > 2 else ""
            day = _as_day(raw)
            if not _in_window(day, start, end):
                continue
            coords.append((x_of(day), y_of(int(miles)), day.isoformat(), int(miles), extra))
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y, _, _, _) in enumerate(coords)
        )
        drawn.append(
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "d": path,
                "dots": [
                    {
                        "cx": x,
                        "cy": y,
                        "date": day,
                        "label": format_br_date(day),
                        "miles": miles,
                        "flight": extra,
                    }
                    for x, y, day, miles, extra in coords
                ],
            }
        )
    tick_days: list[date] = []
    seen_ticks: set[date] = set()
    for part in (0, 0.25, 0.5, 0.75, 1):
        day = start + timedelta(days=round(span_days * part))
        if day not in seen_ticks:
            seen_ticks.add(day)
            tick_days.append(day)
    y_ticks = sorted({low, low + span // 2, high})
    today = date.today()
    today_in = _in_window(today, start, end)
    return {
        "width": width,
        "height": height,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series": drawn,
        "x_ticks": [{"x": x_of(day), "label": format_br_date(day.isoformat())} for day in tick_days],
        "y_ticks": [{"value": value, "y": y_of(value)} for value in y_ticks],
        "today_x": x_of(today) if today_in else None,
        "min": low,
        "max": high,
        "pad_left": pad_left,
        "pad_right": pad_right,
        "has_points": any(item["dots"] for item in drawn),
    }


def dashboard_payload(
    origin: str,
    destination: str | None = None,
    travel_date: str | None = None,
    program: str | None = None,
    span_key: str | None = None,
) -> dict[str, Any]:
    key = span_key if span_key in CHART_SPANS else "ano"
    span_label, span_days = CHART_SPANS[key]
    today = date.today()
    start = today - timedelta(days=span_days)
    end = today + timedelta(days=span_days)
    all_origin = list_snapshots(origin=origin, miles_program=program or None)
    dests: dict[str, str] = {}
    for row in all_origin:
        code = str(row.get("destination") or "")
        if code:
            dests[code] = city_name(code)
    selected = destination or ""
    if not selected and dests:
        selected = next(iter(sorted(dests, key=lambda code: dests[code])))
    route_snaps = [row for row in all_origin if not selected or row["destination"] == selected]
    dated: list[dict[str, Any]] = []
    all_travel_map = _travel_index(route_snaps)
    if travel_date:
        dated = [row for row in route_snaps if str(row.get("departure_date") or "")[:10] == travel_date[:10]]
        best_days = _best_by_day(dated)
        travel_map = _travel_index(dated)
        hist_rows = dated
    else:
        best_days = _best_by_day(route_snaps)
        travel_map = all_travel_map
        hist_rows = route_snaps
    collection_points = [
        (day, int(row["miles"]), str(row.get("departure_date") or "")[:10])
        for day, row in sorted(best_days.items())
        if _in_window(_as_day(day), start, today)
    ]
    history = []
    for flight, days in sorted(_by_flight_days(hist_rows).items()):
        seq = [(day, row) for day, row in sorted(days.items()) if _in_window(_as_day(day), start, today)]
        if not seq:
            continue
        last_day, last_row = seq[-1]
        previous_row = seq[-2][1] if len(seq) >= 2 else None
        prev_miles = int(previous_row["miles"]) if previous_row else None
        miles = int(last_row["miles"])
        history.append(
            {
                "day": last_day,
                "prev_day": seq[-2][0] if previous_row else None,
                "miles": miles,
                "flight": flight,
                "origin": last_row.get("origin"),
                "destination": last_row.get("destination"),
                "prev": prev_miles,
                "fell": prev_miles is not None and miles < prev_miles,
                "rose": prev_miles is not None and miles > prev_miles,
            }
        )
    travel_points = [
        (day, miles)
        for day, miles in sorted(travel_map.items())
        if _in_window(_as_day(day), start, end)
    ]
    chart = chart_timed(
        [
            {"key": "coleta", "label": "Nas coletas", "points": collection_points},
            {"key": "voo", "label": "Por data do voo", "points": travel_points},
        ],
        start,
        end,
    )
    moves = []
    for dest, city in dests.items():
        dest_rows = [row for row in all_origin if row["destination"] == dest]
        for flight, days in _by_flight_days(dest_rows).items():
            move = _move_from_best_days(days)
            if not move:
                continue
            moves.append(
                {
                    "origin": origin,
                    "destination": dest,
                    "city": city,
                    "flight": flight,
                    **move,
                }
            )
    moves.sort(key=lambda item: item["delta"])
    home = city_group(origin)
    all_snaps = list_snapshots(miles_program=program or None)
    round_moves = []
    seen_groups: set[frozenset[str]] = set()
    for dest, city in dests.items():
        dest_group = frozenset(city_group(dest))
        if dest_group & home or dest_group in seen_groups:
            continue
        seen_groups.add(dest_group)
        label = "SAO" if "SAO" in dest_group else dest
        out_flights = _by_flight_days(
            [row for row in all_origin if str(row.get("destination") or "") in dest_group]
        )
        in_flights = _by_flight_days(
            [
                row
                for row in all_snaps
                if str(row.get("origin") or "") in dest_group
                and str(row.get("destination") or "") in home
            ]
        )
        for out_day, out_days in out_flights.items():
            for in_day, in_days in in_flights.items():
                common = sorted(set(out_days) & set(in_days))
                if len(common) < 2:
                    continue
                prev_collect, last_collect = common[-2], common[-1]
                prev_out, last_out = out_days[prev_collect], out_days[last_collect]
                prev_in, last_in = in_days[prev_collect], in_days[last_collect]
                was = int(prev_out["miles"]) + int(prev_in["miles"])
                now = int(last_out["miles"]) + int(last_in["miles"])
                move = _move(was, now)
                if not move:
                    continue
                round_moves.append(
                    {
                        "origin": origin,
                        "destination": label,
                        "city": city_name(label),
                        **move,
                        "was_collected": prev_collect,
                        "now_collected": last_collect,
                        "was_flight": out_day,
                        "now_flight": out_day,
                        "was_return": in_day,
                        "now_return": in_day,
                        "was_out": int(prev_out["miles"]),
                        "now_out": int(last_out["miles"]),
                        "was_in": int(prev_in["miles"]),
                        "now_in": int(last_in["miles"]),
                    }
                )
    round_moves.sort(key=lambda item: item["delta"])
    return {
        "origin": origin,
        "destination": selected,
        "destination_city": dests.get(selected, selected),
        "destinations": sorted(dests.items(), key=lambda item: item[1]),
        "travel_date": (travel_date or "")[:10],
        "travel_dates": [day for day, _ in sorted(all_travel_map.items())],
        "span": key,
        "span_label": span_label,
        "spans": [{"key": span, "label": label} for span, (label, _) in CHART_SPANS.items()],
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "chart": chart,
        "history": history,
        "moves": moves[:24],
        "round_moves": round_moves[:24],
    }


def _latest_by_route(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = _snapshot_key(row)
        if not key:
            continue
        current = latest.get(key)
        if current is None or str(row.get("collected_at") or "") >= str(current.get("collected_at") or ""):
            latest[key] = row
    return list(latest.values())


def _first_miles(rows: list[dict[str, Any]], row: dict[str, Any]) -> int | None:
    key = _snapshot_key(row)
    if not key:
        return None
    series = [item for item in rows if _snapshot_key(item) == key]
    if not series:
        return None
    series.sort(key=lambda item: str(item.get("collected_at") or ""))
    return int(series[0]["miles"])


def backfill_price_history() -> int:
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) AS total FROM price_snapshots").fetchone()
        if count and int(count["total"] or 0) > 0:
            return 0
    rows = query_results(price_type="miles", limit=20000)
    saved = record_cheapest_from_rows(rows)
    saved += _backfill_from_harvest()
    return saved


def _backfill_from_harvest() -> int:
    if not HARVEST_DIR.exists():
        return 0
    rows: list[dict[str, Any]] = []
    for path in HARVEST_DIR.rglob("*.json"):
        parsed = _harvest_file_row(path)
        if parsed:
            rows.append(parsed)
    return record_cheapest_from_rows(rows)


def _harvest_file_row(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    offers = payload.get("offers") or []
    if payload.get("status") not in {200, "ok", "empty"} or not offers:
        return None
    cheapest = None
    for offer in offers:
        miles = offer.get("milhas") if offer.get("milhas") is not None else offer.get("miles")
        if miles is None:
            continue
        if cheapest is None or int(miles) < int(cheapest.get("milhas") or cheapest.get("miles") or 10**12):
            cheapest = offer
    if cheapest is None:
        return None
    miles = cheapest.get("milhas") if cheapest.get("milhas") is not None else cheapest.get("miles")
    job = payload.get("job") or {}
    collected = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    return {
        "origin": str(cheapest.get("origin") or job.get("origin") or "").upper(),
        "destination": str(cheapest.get("destination") or job.get("destination") or "").upper(),
        "departure_date": str(cheapest.get("departure_date") or job.get("day") or "")[:10],
        "miles": int(miles),
        "miles_program": str(cheapest.get("program") or job.get("program") or "").lower(),
        "trip_kind": str(cheapest.get("trip_kind") or cheapest.get("trecho") or job.get("leg") or "ida").lower(),
        "found_at": collected,
    }
