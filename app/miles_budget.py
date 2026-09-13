from __future__ import annotations

from app.airports import is_national
from app.config import MIN_PUBLISH_MILES

LATAM_NATIONAL_MAX = 25_000
LATAM_INTL_LIGHT_MAX = 150_000
LATAM_INTL_BUSINESS_MAX = 300_000
AZUL_NATIONAL_NORMAL_MAX = 30_000
AZUL_INTL_CHEAPEST_MAX = 100_000
AZUL_INTL_BUSINESS_MAX = 150_000
AZUL_FARE_PUBLICA = "Tarifa Pública"
AZUL_FARE_DIAMANTE = "Tarifa Diamante"
AZUL_FARE_UNICA = "Tarifa"


def azul_fare_pairs(normal: int | None, diamond: int | None) -> list[tuple[str, int]]:
    public = int(normal) if normal else None
    discounted = int(diamond) if diamond else None
    if public and discounted and public != discounted:
        return [(AZUL_FARE_PUBLICA, public), (AZUL_FARE_DIAMANTE, discounted)]
    if public:
        return [(AZUL_FARE_UNICA, public)]
    if discounted:
        return [(AZUL_FARE_UNICA, discounted)]
    return []


def _international(origin: str, dest: str) -> bool:
    return not (is_national(origin) and is_national(dest))


def _is_business(cabin: str | None, fare: str | None) -> bool:
    blob = f"{cabin or ''} {fare or ''}".lower()
    return "business" in blob or "execut" in blob


def min_miles_per_leg(origin: str, dest: str, cabin: str = "economy") -> int:
    international = _international(origin, dest)
    if (cabin or "economy") == "business":
        return 25000 if international else 12000
    return 12000 if international else MIN_PUBLISH_MILES


def max_miles_per_leg(
    origin: str,
    dest: str,
    *,
    program: str = "latam",
    cabin: str = "economy",
    fare: str | None = None,
    trip_kind: str | None = None,
) -> int | None:
    if (trip_kind or "") == "round_trip":
        return None
    international = _international(origin, dest)
    prog = (program or "latam").lower()
    if prog == "azul":
        if not international:
            return AZUL_NATIONAL_NORMAL_MAX
        if _is_business(cabin, fare):
            return AZUL_INTL_BUSINESS_MAX
        return AZUL_INTL_CHEAPEST_MAX
    if not international:
        return LATAM_NATIONAL_MAX
    if _is_business(cabin, fare):
        return LATAM_INTL_BUSINESS_MAX
    return LATAM_INTL_LIGHT_MAX


def plausible_miles(
    miles: int | None,
    origin: str,
    dest: str,
    cabin: str = "economy",
    *,
    program: str = "latam",
    fare: str | None = None,
    trip_kind: str | None = None,
) -> bool:
    if not miles:
        return False
    value = int(miles)
    if value < min_miles_per_leg(origin, dest, cabin):
        return False
    cap = max_miles_per_leg(
        origin,
        dest,
        program=program,
        cabin=cabin,
        fare=fare,
        trip_kind=trip_kind,
    )
    if cap is not None and value > cap:
        return False
    return True
