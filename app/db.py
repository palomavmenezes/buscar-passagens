from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from app.airports import related_airports
from app.config import DB_PATH

CIA_MILES_FILTER = (
    "(price_type != 'miles' OR IFNULL(miles_program, '') IN ('smiles', 'azul', 'latam'))"
    " AND IFNULL(source, '') NOT IN ('seats.aero', 'demo')"
    " AND (price_type != 'miles' OR IFNULL(miles, 0) >= 5000)"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    origins TEXT NOT NULL,
    destinations TEXT NOT NULL,
    trip_type TEXT NOT NULL,
    date_mode TEXT NOT NULL,
    date_start TEXT,
    date_end TEXT,
    stay_nights INTEGER,
    cabin TEXT NOT NULL,
    include_cash INTEGER NOT NULL,
    include_miles INTEGER NOT NULL,
    demo INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    progress TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    return_date TEXT,
    airline TEXT,
    stops INTEGER,
    cabin TEXT,
    price_type TEXT NOT NULL,
    currency TEXT,
    price_cash REAL,
    miles INTEGER,
    miles_program TEXT,
    taxes REAL,
    source TEXT,
    booking_url TEXT,
    found_at TEXT NOT NULL,
    departure_time TEXT,
    arrival_time TEXT,
    duration TEXT,
    operators TEXT,
    layover TEXT,
    return_time TEXT,
    return_arrival TEXT,
    fare TEXT,
    trip_kind TEXT,
    FOREIGN KEY (search_id) REFERENCES searches(id)
);

CREATE INDEX IF NOT EXISTS idx_results_search ON results(search_id);
CREATE INDEX IF NOT EXISTS idx_results_price ON results(price_cash);
CREATE INDEX IF NOT EXISTS idx_results_miles ON results(miles);
CREATE INDEX IF NOT EXISTS idx_results_route ON results(origin, destination);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    status TEXT NOT NULL DEFAULT 'pending',
    approved_at TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(results)")}
        if "departure_time" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN departure_time TEXT")
        if "arrival_time" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN arrival_time TEXT")
        if "duration" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN duration TEXT")
        if "operators" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN operators TEXT")
        if "layover" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN layover TEXT")
        if "return_time" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN return_time TEXT")
        if "return_arrival" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN return_arrival TEXT")
        if "fare" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN fare TEXT")
        if "trip_kind" not in columns:
            db.execute("ALTER TABLE results ADD COLUMN trip_kind TEXT")
        db.execute("DELETE FROM results WHERE IFNULL(source, '') IN ('demo', 'seats.aero')")
        db.execute("DELETE FROM results WHERE price_type = 'cash'")
        db.execute("DELETE FROM searches WHERE demo = 1")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                status TEXT NOT NULL DEFAULT 'pending',
                approved_at TEXT
            )
            """
        )
        user_cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "status" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if "approved_at" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN approved_at TEXT")
        db.execute("UPDATE users SET status = 'approved' WHERE role = 'admin' AND IFNULL(status, '') != 'approved'")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_search(payload: dict[str, Any]) -> int:
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO searches (
                created_at, origins, destinations, trip_type, date_mode,
                date_start, date_end, stay_nights, cabin, include_cash,
                include_miles, demo, status, progress
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'Na fila')
            """,
            (
                now_iso(),
                ",".join(payload["origins"]),
                ",".join(payload["destinations"]),
                payload["trip_type"],
                payload["date_mode"],
                payload.get("date_start"),
                payload.get("date_end"),
                payload.get("stay_nights"),
                payload["cabin"],
                1 if payload["include_cash"] else 0,
                1 if payload["include_miles"] else 0,
                0,
            ),
        )
        return int(cur.lastrowid)


def update_search(search_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE searches SET {assignments} WHERE id = ?",
            (*fields.values(), search_id),
        )


def get_search(search_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        return dict(row) if row else None


def list_searches(limit: int = 40) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM searches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def insert_results(rows: Iterable[dict[str, Any]]) -> int:
    payload = list(rows)
    if not payload:
        return 0
    for row in payload:
        row.setdefault("departure_time", None)
        row.setdefault("arrival_time", None)
        row.setdefault("duration", None)
        row.setdefault("operators", None)
        row.setdefault("layover", None)
        row.setdefault("return_time", None)
        row.setdefault("return_arrival", None)
        row.setdefault("fare", None)
        row.setdefault("trip_kind", None)
        row.pop("segments", None)
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO results (
                search_id, origin, destination, departure_date, return_date,
                airline, stops, cabin, price_type, currency, price_cash,
                miles, miles_program, taxes, source, booking_url, found_at,
                departure_time, arrival_time, duration, operators, layover,
                return_time, return_arrival, fare, trip_kind
            ) VALUES (
                :search_id, :origin, :destination, :departure_date, :return_date,
                :airline, :stops, :cabin, :price_type, :currency, :price_cash,
                :miles, :miles_program, :taxes, :source, :booking_url, :found_at,
                :departure_time, :arrival_time, :duration, :operators, :layover,
                :return_time, :return_arrival, :fare, :trip_kind
            )
            """,
            payload,
        )
        return len(payload)


def _code_filter(column: str, code: str | None) -> tuple[str, list[str]]:
    codes = related_airports(code)
    if not codes:
        return "", []
    if len(codes) == 1:
        return f"{column} = ?", codes
    placeholders = ", ".join("?" for _ in codes)
    return f"{column} IN ({placeholders})", codes


def query_results(
    search_id: int | None = None,
    origin: str | None = None,
    destination: str | None = None,
    price_type: str | None = None,
    cabin: str | None = None,
    trip_kind: str | None = None,
    sort: str = "cheapest",
    limit: int = 4000,
    miles_program: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    airline: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1", CIA_MILES_FILTER]
    params: list[Any] = []
    if search_id is not None:
        clauses.append("search_id = ?")
        params.append(search_id)
    origin_sql, origin_params = _code_filter("origin", origin)
    if origin_sql:
        clauses.append(origin_sql)
        params.extend(origin_params)
    dest_sql, dest_params = _code_filter("destination", destination)
    if dest_sql:
        clauses.append(dest_sql)
        params.extend(dest_params)
    if price_type in {"cash", "miles"}:
        clauses.append("price_type = ?")
        params.append(price_type)
    if cabin and cabin != "all":
        clauses.append("cabin = ?")
        params.append(cabin)
    if trip_kind == "one_way":
        clauses.append("IFNULL(trip_kind, 'ida') IN ('ida', 'volta')")
    elif trip_kind in {"ida", "volta", "round_trip"}:
        clauses.append("IFNULL(trip_kind, 'round_trip') = ?")
        params.append(trip_kind)
    if miles_program:
        clauses.append("IFNULL(miles_program, '') = ?")
        params.append(miles_program.lower())
    if date_from:
        clauses.append("departure_date >= ?")
        params.append(date_from[:10])
    if date_to:
        clauses.append("departure_date <= ?")
        params.append(date_to[:10])
    if airline:
        clauses.append("LOWER(IFNULL(airline, '')) LIKE ?")
        params.append(f"%{airline.lower()}%")

    order = {
        "cheapest": "CASE WHEN price_type = 'cash' THEN IFNULL(price_cash, 999999999) ELSE IFNULL(miles, 999999999) END ASC, departure_date",
        "miles": "IFNULL(miles, 999999999) ASC, departure_date",
        "cash": "IFNULL(price_cash, 999999999) ASC, departure_date",
        "date": "departure_date ASC, IFNULL(price_cash, 999999999) ASC",
        "recent": "found_at DESC",
    }.get(sort, "departure_date ASC")

    sql = f"""
        SELECT * FROM results
        WHERE {' AND '.join(clauses)}
        ORDER BY {order}
        LIMIT ?
    """
    params.append(limit)
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def result_stats(
    search_id: int | None = None,
    price_type: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
) -> dict[str, Any]:
    clauses = ["1=1", CIA_MILES_FILTER]
    params: list[Any] = []
    if search_id is not None:
        clauses.append("search_id = ?")
        params.append(search_id)
    origin_sql, origin_params = _code_filter("origin", origin)
    if origin_sql:
        clauses.append(origin_sql)
        params.extend(origin_params)
    dest_sql, dest_params = _code_filter("destination", destination)
    if dest_sql:
        clauses.append(dest_sql)
        params.extend(dest_params)
    if price_type in {"cash", "miles"}:
        clauses.append("price_type = ?")
        params.append(price_type)
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as db:
        row = db.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN price_type = 'cash' THEN 1 ELSE 0 END) AS cash_count,
                SUM(CASE WHEN price_type = 'miles' THEN 1 ELSE 0 END) AS miles_count,
                MIN(price_cash) AS min_cash,
                MIN(miles) AS min_miles
            FROM results
            {where}
            """,
            params,
        ).fetchone()
        return dict(row)


def cheapest_destinations(origin: str) -> list[dict[str, Any]]:
    origin_sql, origin_params = _code_filter("origin", origin)
    row_origin_sql, _ = _code_filter("r.origin", origin)
    if not origin_sql:
        return []
    with get_db() as db:
        rows = db.execute(
            """
            SELECT r.*
            FROM results r
            JOIN (
                SELECT destination, MIN(miles) AS best
                FROM results
                WHERE {origin_sql} AND price_type = 'miles' AND miles IS NOT NULL
                  AND {filter}
                  AND (
                    trip_kind = 'round_trip'
                    OR (
                      trip_kind = 'ida'
                      AND destination NOT IN (
                        SELECT destination FROM results
                        WHERE {origin_sql} AND price_type = 'miles' AND miles IS NOT NULL
                          AND {filter}
                          AND trip_kind = 'round_trip'
                      )
                    )
                  )
                GROUP BY destination
            ) best_rows
              ON r.destination = best_rows.destination
             AND r.miles = best_rows.best
            WHERE {row_origin_sql} AND r.price_type = 'miles'
              AND {filter}
              AND IFNULL(r.trip_kind, '') IN ('ida', 'round_trip')
            GROUP BY r.destination
            ORDER BY r.miles ASC
            """.format(filter=CIA_MILES_FILTER, origin_sql=origin_sql, row_origin_sql=row_origin_sql),
            (*origin_params, *origin_params, *origin_params),
        ).fetchall()
        return [dict(row) for row in rows]


def query_route_pair(
    origin: str,
    destination: str,
    price_type: str | None = "miles",
    cabin: str | None = None,
    trip_kind: str | None = None,
    sort: str = "miles",
    limit: int = 4000,
) -> list[dict[str, Any]]:
    outbound = query_results(
        origin=origin,
        destination=destination,
        price_type=price_type,
        cabin=cabin,
        trip_kind=trip_kind,
        sort=sort,
        limit=limit,
    )
    if not origin or not destination:
        return outbound
    inbound = query_results(
        origin=destination,
        destination=origin,
        price_type=price_type,
        cabin=cabin,
        trip_kind=trip_kind,
        sort=sort,
        limit=limit,
    )
    seen = {row.get("id") for row in outbound}
    merged = list(outbound)
    for row in inbound:
        if row.get("id") not in seen:
            merged.append(row)
            seen.add(row.get("id"))
    order = {
        "cheapest": lambda row: (row.get("miles") if row.get("miles") is not None else 10**12, row.get("departure_date") or ""),
        "miles": lambda row: (row.get("miles") if row.get("miles") is not None else 10**12, row.get("departure_date") or ""),
        "date": lambda row: (row.get("departure_date") or "", row.get("miles") if row.get("miles") is not None else 10**12),
        "recent": lambda row: row.get("found_at") or "",
    }.get(sort, lambda row: (row.get("miles") if row.get("miles") is not None else 10**12, row.get("departure_date") or ""))
    reverse = sort == "recent"
    merged.sort(key=order, reverse=reverse)
    return merged[:limit]


def replace_miles_route(
    origin: str,
    destination: str,
    departure_date: str,
    miles_program: str,
    cabin: str | None = None,
    return_date: str | None = None,
) -> None:
    with get_db() as db:
        sql = """
            DELETE FROM results
            WHERE origin = ?
              AND destination = ?
              AND departure_date = ?
              AND IFNULL(miles_program, '') = ?
              AND price_type = 'miles'
            """
        params: list[Any] = [origin.upper(), destination.upper(), departure_date[:10], miles_program]
        if cabin:
            sql += " AND IFNULL(cabin, '') = ?"
            params.append(cabin)
        db.execute(sql, params)
        if return_date:
            volta_sql = """
                DELETE FROM results
                WHERE origin = ?
                  AND destination = ?
                  AND departure_date = ?
                  AND IFNULL(miles_program, '') = ?
                  AND price_type = 'miles'
                  AND IFNULL(trip_kind, '') = 'volta'
                """
            volta_params: list[Any] = [
                destination.upper(),
                origin.upper(),
                return_date[:10],
                miles_program,
            ]
            if cabin:
                volta_sql += " AND IFNULL(cabin, '') = ?"
                volta_params.append(cabin)
            db.execute(volta_sql, volta_params)


def count_users() -> int:
    with get_db() as db:
        row = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row["total"] if row else 0)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(
    name: str,
    email: str,
    password_hash: str,
    role: str = "viewer",
    status: str = "pending",
) -> dict[str, Any]:
    status = status if status in {"pending", "approved", "rejected"} else "pending"
    role = role if role in {"admin", "viewer"} else "viewer"
    if role == "admin":
        status = "approved"
    approved_at = now_iso() if status == "approved" else None
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO users (created_at, name, email, password_hash, role, status, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), name.strip(), email.strip().lower(), password_hash, role, status, approved_at),
        )
        row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def list_users() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT id, created_at, name, email, role, status, approved_at FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def count_pending_users() -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total FROM users WHERE status = 'pending' AND role != 'admin'"
        ).fetchone()
        return int(row["total"] if row else 0)


def count_admins() -> int:
    with get_db() as db:
        row = db.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'admin'").fetchone()
        return int(row["total"] if row else 0)


def update_user(user_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_user_by_id(user_id)
    allowed = {"name", "role", "status", "approved_at", "password_hash"}
    payload = {key: value for key, value in fields.items() if key in allowed}
    if not payload:
        return get_user_by_id(user_id)
    assignments = ", ".join(f"{key} = ?" for key in payload)
    with get_db() as db:
        db.execute(
            f"UPDATE users SET {assignments} WHERE id = ?",
            (*payload.values(), user_id),
        )
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def public_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "status": row.get("status") or "pending",
    }


def delete_miles_program(program: str) -> int:
    with get_db() as db:
        cur = db.execute(
            """
            DELETE FROM results
            WHERE price_type = 'miles'
              AND IFNULL(miles_program, '') = ?
            """,
            (program.lower(),),
        )
        return int(cur.rowcount or 0)
