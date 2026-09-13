from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Offer:
    origin: str
    destination: str
    departure_date: str
    return_date: str | None
    airline: str | None
    stops: int | None
    cabin: str
    price_type: str
    currency: str | None
    price_cash: float | None
    miles: int | None
    miles_program: str | None
    taxes: float | None
    source: str
    booking_url: str | None
    departure_time: str | None = None
    arrival_time: str | None = None
    duration: str | None = None
    operators: str | None = None
    layover: str | None = None
    return_time: str | None = None
    return_arrival: str | None = None
    fare: str | None = None
    segments: list[dict[str, Any]] | None = None
    trip_kind: str = "round_trip"

    def as_row(self, search_id: int, found_at: str) -> dict[str, Any]:
        data = asdict(self)
        data["search_id"] = search_id
        data["found_at"] = found_at
        data["origin"] = self.origin.upper()
        data["destination"] = self.destination.upper()
        return data
