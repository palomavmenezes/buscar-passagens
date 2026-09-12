from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from app.config import DB_PATH

CIA_MILES_FILTER = (
    "(price_type != 'miles' OR IFNULL(miles_program, '') IN ('smiles', 'azul', 'latam'))"
    " AND IFNULL(source, '') NOT IN ('seats.aero', 'demo')"
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
    FOREIGN KEY (search_id) REFERENCES searches(id)
);

CREATE INDEX IF NOT EXISTS idx_results_search ON results(search_id);
CREATE INDEX IF NOT EXISTS idx_results_price ON results(price_cash);
CREATE INDEX IF NOT EXISTS idx_results_miles ON results(miles);
CREATE INDEX IF NOT EXISTS idx_results_route ON results(origin, destination);
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
        db.execute("DELETE FROM results WHERE IFNULL(source, '') IN ('demo', 'seats.aero')")
        db.execute("DELETE FROM results WHERE price_type = 'cash'")
        db.execute("DELETE FROM searches WHERE demo = 1")


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
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO results (
                search_id, origin, destination, departure_date, return_date,
                airline, stops, cabin, price_type, currency, price_cash,
                miles, miles_program, taxes, source, booking_url, found_at,
                departure_time, arrival_time
            ) VALUES (
                :search_id, :origin, :destination, :departure_date, :return_date,
                :airline, :stops, :cabin, :price_type, :currency, :price_cash,
                :miles, :miles_program, :taxes, :source, :booking_url, :found_at,
                :departure_time, :arrival_time
            )
            """,
            payload,
        )
        return len(payload)


def query_results(
    search_id: int | None = None,
    origin: str | None = None,
    destination: str | None = None,
    price_type: str | None = None,
    cabin: str | None = None,
    sort: str = "cheapest",
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses = ["1=1", CIA_MILES_FILTER]
    params: list[Any] = []
    if search_id is not None:
        clauses.append("search_id = ?")
        params.append(search_id)
    if origin:
        clauses.append("origin = ?")
        params.append(origin.upper())
    if destination:
        clauses.append("destination = ?")
        params.append(destination.upper())
    if price_type in {"cash", "miles"}:
        clauses.append("price_type = ?")
        params.append(price_type)
    if cabin and cabin != "all":
        clauses.append("cabin = ?")
        params.append(cabin)

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
    if origin:
        clauses.append("origin = ?")
        params.append(origin.upper())
    if destination:
        clauses.append("destination = ?")
        params.append(destination.upper())
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
    with get_db() as db:
        rows = db.execute(
            """
            SELECT r.*
            FROM results r
            JOIN (
                SELECT destination, MIN(miles) AS best
                FROM results
                WHERE origin = ? AND price_type = 'miles' AND miles IS NOT NULL
                  AND {filter}
                GROUP BY destination
            ) best_rows
              ON r.destination = best_rows.destination
             AND r.miles = best_rows.best
            WHERE r.origin = ? AND r.price_type = 'miles'
              AND {filter}
            GROUP BY r.destination
            ORDER BY r.miles ASC
            """.format(filter=CIA_MILES_FILTER),
            (origin.upper(), origin.upper()),
        ).fetchall()
        return [dict(row) for row in rows]


def replace_miles_route(
    origin: str,
    destination: str,
    departure_date: str,
    miles_program: str,
) -> None:
    with get_db() as db:
        db.execute(
            """
            DELETE FROM results
            WHERE origin = ?
              AND destination = ?
              AND departure_date = ?
              AND IFNULL(miles_program, '') = ?
              AND price_type = 'miles'
            """,
            (origin.upper(), destination.upper(), departure_date[:10], miles_program),
        )
