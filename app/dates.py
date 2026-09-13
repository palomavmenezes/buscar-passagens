from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from app.config import MAX_FLEX_DAYS, MAX_YEAR_DAYS


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def horizon_days(date_mode: str) -> int:
    if date_mode in {"year", "month", "specific"}:
        return MAX_YEAR_DAYS
    return MAX_FLEX_DAYS


def clamp_horizon(start: date, end: date, max_days: int = MAX_FLEX_DAYS) -> tuple[date, date]:
    today = date.today()
    earliest = today + timedelta(days=1)
    latest = today + timedelta(days=max_days)
    start = max(start, earliest)
    end = min(end, latest)
    if end < start:
        end = start
    return start, end


def month_bounds(year_month: str) -> tuple[date, date]:
    year, month = [int(part) for part in year_month.split("-")]
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_sample_dates(year_month: str) -> list[date]:
    start, end = month_bounds(year_month[:7])
    last = end.day
    first = min(10, last)
    second = min(20, last)
    days = [date(start.year, start.month, first)]
    if second != first:
        days.append(date(start.year, start.month, second))
    return days


def friday_monday_weekends(year_month: str) -> list[tuple[date, date]]:
    start, end = month_bounds(year_month[:7])
    pairs: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() == 4:
            monday = cursor + timedelta(days=3)
            pairs.append((cursor, monday))
        cursor += timedelta(days=1)
    return pairs


def resolve_window(
    date_mode: str,
    date_start: str | None,
    date_end: str | None,
) -> tuple[date, date]:
    today = date.today()
    max_days = horizon_days(date_mode)
    if date_mode == "specific":
        start = parse_date(date_start) or (today + timedelta(days=1))
        end = parse_date(date_end) or start
        return clamp_horizon(start, end, max_days)
    if date_mode == "month" and date_start:
        start, end = month_bounds(date_start[:7])
        return clamp_horizon(start, end, max_days)
    start = parse_date(date_start) or (today + timedelta(days=1))
    span = MAX_YEAR_DAYS if date_mode == "year" else MAX_FLEX_DAYS
    end = parse_date(date_end) or (start + timedelta(days=span - 1))
    return clamp_horizon(start, end, max_days)


def sample_dates(start: date, end: date, step_days: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=step_days)
    if days[-1] != end:
        days.append(end)
    return days


def format_br_date(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value)[:10]
    if len(text) == 10 and text[4] == "-":
        year, month, day = text.split("-")
        return f"{day}/{month}/{year}"
    return str(value)


def format_updated(found_at: str | None) -> str:
    if not found_at:
        return "sem atualização"
    text = str(found_at).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return format_br_date(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = int((now - parsed.astimezone(timezone.utc)).total_seconds())
    if seconds < 45:
        return "agora"
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"há {minutes} min"
    if seconds < 86400:
        hours = seconds // 3600
        return f"há {hours} h"
    days = seconds // 86400
    if days == 1:
        return "há 1 dia"
    if days < 15:
        return f"há {days} dias"
    return format_br_date(text)


def format_clock_label(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if len(text) >= 5 and text[2] == ":":
        return f"{text[:2]}h{text[3:5]}"
    return text


def format_when(day: str | None, time: str | None = None) -> str:
    if not day:
        return "—"
    label = format_br_date(day)
    try:
        weekday = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")[date.fromisoformat(str(day)[:10]).weekday()]
        label = f"{weekday} {label}"
    except ValueError:
        pass
    clock = format_clock_label(time)
    if clock:
        return f"{label} · {clock}"
    return label


def format_flight_span(day: str | None, departure: str | None = None, arrival: str | None = None) -> str:
    label = format_when(day, departure)
    clock = format_clock_label(arrival)
    if clock:
        return f"{label} → {clock}"
    return label
