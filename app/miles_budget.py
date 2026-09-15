from __future__ import annotations

from app.airports import is_national
from app.config import MIN_PUBLISH_MILES

LATAM_NATIONAL_MAX = 25_000
LATAM_INTL_LIGHT_MAX = 150_000
LATAM_INTL_BUSINESS_MAX = 300_000
AZUL_NATIONAL_NORMAL_MAX = 25_000
EXCELLENT_MILES = 8_000
AZUL_INTL_CHEAPEST_MAX = 100_000
AZUL_INTL_BUSINESS_MAX = 150_000
AZUL_FARE_PUBLICA = "Tarifa Pública"
AZUL_FARE_DIAMANTE = "Tarifa Diamante"
AZUL_FARE_UNICA = "Tarifa"
SMILES_NATIONAL_MILES_MAX = 15_000
SMILES_INTL_MILES_MAX = 50_000
SMILES_NATIONAL_CASH_MAX = 400.0
SMILES_INTL_CASH_MAX = 1_000.0
SMILES_MILES_MAX = SMILES_NATIONAL_MILES_MAX
SMILES_CASH_MAX = SMILES_NATIONAL_CASH_MAX
SMILES_FARE_CLIENT = "Tarifa Smiles"
SMILES_FARE_CLUB = "Tarifa Clube Smiles"
SMILES_FARE_CASH = "Dinheiro"


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
    if prog == "smiles":
        return SMILES_INTL_MILES_MAX if international else SMILES_NATIONAL_MILES_MAX
    if not international:
        return LATAM_NATIONAL_MAX
    if _is_business(cabin, fare):
        return LATAM_INTL_BUSINESS_MAX
    return LATAM_INTL_LIGHT_MAX


def total_miles_budget(
    origin: str,
    dest: str,
    *,
    program: str = "latam",
    cabin: str = "economy",
    fare: str | None = None,
    trip_type: str = "one_way",
) -> tuple[int, int]:
    legs = 2 if trip_type == "round_trip" else 1
    low = min_miles_per_leg(origin, dest, cabin or "economy") * legs
    cap = max_miles_per_leg(
        origin,
        dest,
        program=program,
        cabin=cabin or "economy",
        fare=fare,
        trip_kind=None,
    )
    high = (cap if cap is not None else LATAM_INTL_LIGHT_MAX) * legs
    return low, high


def miles_within_budget(
    miles: int | None,
    origin: str,
    dest: str,
    *,
    program: str = "latam",
    cabin: str = "economy",
    fare: str | None = None,
    trip_type: str = "one_way",
    miles_min: int | None = None,
    miles_max: int | None = None,
) -> bool:
    if not miles:
        return False
    value = int(miles)
    low, high = total_miles_budget(
        origin,
        dest,
        program=program,
        cabin=cabin,
        fare=fare,
        trip_type=trip_type,
    )
    if miles_min is not None:
        low = miles_min
    if miles_max is not None:
        high = miles_max
    return low <= value <= high


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


def smiles_caps(origin: str, dest: str) -> tuple[int, float]:
    if _international(origin, dest):
        return SMILES_INTL_MILES_MAX, SMILES_INTL_CASH_MAX
    return SMILES_NATIONAL_MILES_MAX, SMILES_NATIONAL_CASH_MAX


def smiles_keep_offer(
    miles: int | None,
    cash: float | None,
    origin: str = "",
    dest: str = "",
) -> bool:
    """GOL: nacional 5–15 mil milhas ou até R$ 400; internacional 12–50 mil ou até R$ 1.000."""
    miles_cap, cash_cap = smiles_caps(origin, dest)
    floor = min_miles_per_leg(origin, dest)
    if miles:
        value = int(miles)
        return floor <= value <= miles_cap
    if cash is not None:
        try:
            return 0 < float(cash) <= cash_cap
        except (TypeError, ValueError):
            return False
    return False
