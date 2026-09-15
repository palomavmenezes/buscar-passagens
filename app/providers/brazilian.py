from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.airports import CITY_AIRPORTS, collapse_rio_codes, expand_city_airports

BRT = timezone(timedelta(hours=-3))
from app.config import GECKOAPI_API_KEY
from app.dates import sample_dates
from app.miles_budget import azul_fare_pairs
from app.providers.base import Offer

GECKO_URL = "https://api.geckoapi.com.br/v1/extract"
GECKO_CREDITS_URL = "https://api.geckoapi.com.br/v1/me/credits"
TIMEOUT = 90.0
CREDIT_COST = 5


def _num(value) -> float | None:
    if value in (None, "", False):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _miles(value) -> int | None:
    number = _num(value)
    if number is None or number <= 0:
        return None
    return int(number)


def smiles_epoch_ms(day: str, *, noon: bool = False) -> int:
    parsed = date.fromisoformat(day[:10])
    hour = 12 if noon else 0
    return int(datetime(parsed.year, parsed.month, parsed.day, hour, tzinfo=BRT).timestamp() * 1000)


def smiles_url(origin: str, dest: str, day: str, return_date: str | None) -> str:
    origin_u = origin.strip().upper()
    dest_u = dest.strip().upper()
    origin_any = "true" if origin_u in CITY_AIRPORTS else "false"
    dest_any = "true" if dest_u in CITY_AIRPORTS else "false"
    url = (
        "https://www.smiles.com.br/mfe/emissao-passagem/"
        f"?adults=1&cabin=ALL&children=0&departureDate={smiles_epoch_ms(day)}"
        "&infants=0&isElegible=false&isFlexibleDateChecked=false"
    )
    if return_date:
        url += f"&returnDate={smiles_epoch_ms(return_date, noon=True)}&searchType=g3&segments=1&tripType=1"
    else:
        url += "&searchType=g3&segments=1&tripType=2"
    url += (
        f"&originAirport={origin_u}&originCity=&originCountry=&originAirportIsAny={origin_any}"
        f"&destinationAirport={dest_u}&destinCity=&destinCountry=&destinAirportIsAny={dest_any}"
        "&novo-resultado-voos=true"
    )
    return url


def azul_url(origin: str, dest: str, day: str, return_date: str | None = None) -> str:
    pretty = day[5:7] + "%2F" + day[8:10] + "%2F" + day[:4]
    url = (
        "https://www.voeazul.com.br/br/pt/home/selecao-voo"
        f"?c[0].ds={origin}&c[0].as={dest}&c[0].std={pretty}"
        "&p[0].t=ADT&p[0].c=1&p[0].tc=BRL&p[0].cp=true"
    )
    if return_date:
        back = return_date[5:7] + "%2F" + return_date[8:10] + "%2F" + return_date[:4]
        url += f"&c[1].ds={dest}&c[1].as={origin}&c[1].std={back}"
    return url


def latam_url(origin: str, dest: str, day: str, redemption: bool = True, return_date: str | None = None) -> str:
    flag = "true" if redemption else "false"
    inbound = f"&inbound={return_date}T00:00:00.000Z" if return_date else ""
    trip = "RT" if return_date else "OW"
    return (
        "https://www.latamairlines.com/br/pt/oferta-voos"
        f"?origin={origin}&outbound={day}T00:00:00.000Z{inbound}&destination={dest}"
        f"&adt=1&chd=0&inf=0&trip={trip}&cabin=Economy&redemption={flag}&sort=RECOMMENDED"
    )


def gecko_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GECKOAPI_API_KEY}",
        "Content-Type": "application/json",
    }


def build_extract_body(
    program: str,
    origin: str,
    dest: str,
    day: str,
    return_date: str | None = None,
) -> dict:
    body: dict = {
        "type": "plp",
        "from": origin,
        "to": dest,
        "departureDate": day,
        "numAdults": 1,
    }
    if return_date:
        body["returnDate"] = return_date
    if program == "smiles":
        body["target"] = "smiles.com.br"
    elif program == "azul":
        body["target"] = "voeazul.com.br"
        body["currency"] = "BRL"
        body["points"] = True
        body["url"] = azul_url(origin, dest, day)
    else:
        body["target"] = "latamairlines.com"
        body["url"] = latam_url(origin, dest, day, True)
    return body


def parse_extract(
    program: str,
    data: dict,
    origin: str,
    dest: str,
    day: str,
    return_date: str | None,
) -> list[Offer]:
    if program == "smiles":
        return _parse_smiles(data, origin, dest, day, return_date)
    if program == "azul":
        return _parse_azul(data, origin, dest, day, return_date)
    return _parse_latam(data, origin, dest, day, return_date)


async def extract_raw(client: httpx.AsyncClient, body: dict) -> tuple[int, dict]:
    if not GECKOAPI_API_KEY:
        return 401, {"error": "missing_key"}
    response = await client.post(
        GECKO_URL,
        headers=gecko_headers(),
        json=body,
        timeout=TIMEOUT,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"error": "invalid_json", "text": response.text[:2000]}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    return response.status_code, payload


async def _extract(client: httpx.AsyncClient, body: dict) -> dict | None:
    status, payload = await extract_raw(client, body)
    if status >= 400:
        return None
    data = payload.get("data")
    if not data or payload.get("notFound"):
        return None
    return data


def miles_values_from_json(node: Any, found: list[int] | None = None) -> list[int]:
    found = found if found is not None else []
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).lower()
            if name in {"miles", "points", "milhas", "discountedpoints", "faremiles", "smilesmiles"}:
                amount = _miles(value if not isinstance(value, dict) else value.get("amount") or value.get("value"))
                if amount and 500 <= amount <= 9_000_000:
                    found.append(amount)
            else:
                miles_values_from_json(value, found)
    elif isinstance(node, list):
        for child in node:
            miles_values_from_json(child, found)
    return found


def _smiles_best(item: dict) -> tuple[int, float | None] | None:
    best = None
    for option in item.get("fareOptions") or [{}]:
        miles = _miles(option.get("miles") or item.get("priceHint"))
        if miles is None:
            continue
        if best is None or miles < best[0]:
            best = (miles, _num(option.get("costTax")))
    return best


def _is_return_segment(item: dict) -> bool:
    token = str(
        item.get("segment_type")
        or item.get("segmentType")
        or item.get("direction")
        or ""
    ).upper()
    return any(flag in token for flag in ("SEGMENT_2", "RETURN", "INBOUND", "VOLTA"))


def _parse_smiles(data: dict, origin: str, dest: str, day: str, return_date: str | None) -> list[Offer]:
    items = list(data.get("offers") or data.get("requestedFlightSegmentList") or data.get("flights") or [])
    origin_u = origin.upper()
    dest_u = dest.upper()
    offers: list[Offer] = []
    for item in items:
        best = _smiles_best(item)
        if not best:
            continue
        inbound = bool(return_date) and _is_return_segment(item)
        if inbound:
            off_origin, off_dest, dep, kind, ret = dest_u, origin_u, return_date, "volta", None
        else:
            off_origin, off_dest, dep, kind, ret = origin_u, dest_u, day, "ida", return_date
        cabin = str(item.get("cabin") or "ECONOMIC").lower()
        cabin_key = "economy"
        if "business" in cabin or "execut" in cabin:
            cabin_key = "business"
        elif "premium" in cabin:
            cabin_key = "premium"
        stops = item.get("stops") or 0
        try:
            stops = int(stops)
        except (TypeError, ValueError):
            stops = 1
        offers.append(
            Offer(
                origin=off_origin,
                destination=off_dest,
                departure_date=dep,
                return_date=ret,
                airline=str(item.get("airline") or "GOL"),
                stops=stops,
                cabin=cabin_key,
                price_type="miles",
                currency="BRL",
                price_cash=None,
                miles=best[0],
                miles_program="smiles",
                taxes=best[1],
                source="smiles",
                booking_url=smiles_url(off_origin, off_dest, dep, ret),
                trip_kind=kind,
            )
        )
    return offers


def _azul_iata(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and len(value.strip()) == 3:
            token = value.strip().upper()
            if token.isalpha() and token not in {"BRL", "USD", "EUR", "PTS", "ADT"}:
                return token
        if isinstance(value, dict):
            for key in ("iata", "iataCode", "stationCode", "station", "code", "departureStation", "origin"):
                found = _azul_iata(value.get(key))
                if found:
                    return found
    return None


def _money_amount(node: Any) -> float | None:
    if isinstance(node, dict):
        return _num(node.get("amount") or node.get("value"))
    return _num(node)


def _iso_clock(value: Any) -> str | None:
    text = str(value or "")
    if "T" in text:
        clock = text.split("T", 1)[1][:5]
        if len(clock) == 5 and clock[2] == ":":
            return clock
    return None


def _azul_duration(value: Any) -> str | None:
    text = str(value or "")
    if text.startswith("PT"):
        hours = 0
        minutes = 0
        if "H" in text:
            left, text = text.replace("PT", "").split("H", 1)
            hours = int(left or 0)
        text = text.replace("PT", "").replace("M", "")
        if text.isdigit():
            minutes = int(text)
        if hours or minutes:
            return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    return text or None


def _azul_code_set(code: str) -> set[str]:
    token = (code or "").strip().upper()
    return {token, *expand_city_airports(token)}


def _azul_option_cash(option: dict) -> float:
    total = _money_amount(option.get("totalMoney"))
    taxes = _money_amount(option.get("taxesAndFees"))
    if total is None:
        return 0.0
    if taxes is None:
        return float(total)
    return max(0.0, float(total) - float(taxes))


def _azul_journey_fares(journey: dict) -> list[tuple[str, int, float | None]]:
    normal = None
    diamond = None
    for fare in journey.get("fares") or []:
        for option in fare.get("pointsOptions") or []:
            if _azul_option_cash(option) > 80:
                continue
            taxes = _money_amount(option.get("taxesAndFees"))
            full = _miles(option.get("points"))
            disc = _miles(option.get("discountedPoints"))
            if full and (normal is None or full < normal[0]):
                normal = (full, taxes)
            if disc and disc != full and (diamond is None or disc < diamond[0]):
                diamond = (disc, taxes)
            elif disc and not full and (diamond is None or disc < diamond[0]):
                diamond = (disc, taxes)
    rows: list[tuple[str, int, float | None]] = []
    for fare_name, miles in azul_fare_pairs(
        normal[0] if normal else None,
        diamond[0] if diamond else None,
    ):
        taxes = None
        if normal and miles == normal[0]:
            taxes = normal[1]
        elif diamond and miles == diamond[0]:
            taxes = diamond[1]
        rows.append((fare_name, miles, taxes))
    return rows


def _azul_trips(data: dict) -> list[dict]:
    if isinstance(data.get("trips"), list):
        return data["trips"]
    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("trips"), list):
        return nested["trips"]
    return []


def _parse_azul(data: dict, origin: str, dest: str, day: str, return_date: str | None) -> list[Offer]:
    offers: list[Offer] = []
    origin_set = _azul_code_set(origin)
    dest_set = _azul_code_set(dest)
    trips = _azul_trips(data)
    for trip_index, trip in enumerate(trips):
        trip_origin = _azul_iata(trip.get("origin"), trip.get("departureStation"))
        trip_dest = _azul_iata(trip.get("destination"), trip.get("arrivalStation"))
        for journey in trip.get("journeys") or []:
            fares = _azul_journey_fares(journey)
            if not fares:
                continue
            segments = journey.get("segments") or []
            first = segments[0] if segments and isinstance(segments[0], dict) else {}
            last = segments[-1] if segments and isinstance(segments[-1], dict) else {}
            origin_code = _azul_iata(
                journey.get("departureStation"),
                journey.get("origin"),
                first.get("departureStation"),
                first.get("origin"),
                first.get("departure"),
                trip_origin,
                origin,
            ) or origin
            dest_code = _azul_iata(
                journey.get("arrivalStation"),
                journey.get("destination"),
                last.get("arrivalStation"),
                last.get("destination"),
                last.get("arrival"),
                trip_dest,
                dest,
            ) or dest
            inbound = origin_code in dest_set and dest_code in origin_set
            if not inbound and trip_index == 1 and return_date:
                inbound = True
            kind = "volta" if inbound else "ida"
            dep_day = return_date if inbound and return_date else day
            dep_time = _iso_clock(journey.get("departure") or journey.get("std") or first.get("departure"))
            arr_time = _iso_clock(journey.get("arrival") or journey.get("sta") or last.get("arrival"))
            stops = journey.get("stopsCount")
            if stops is None:
                stops = max(0, len(segments) - 1) if segments else None
            duration = _azul_duration(journey.get("duration"))
            for fare_name, miles, taxes in fares:
                offers.append(
                    Offer(
                        origin=origin_code,
                        destination=dest_code,
                        departure_date=dep_day,
                        return_date=return_date if kind == "ida" else None,
                        airline="Azul",
                        stops=int(stops) if stops is not None else None,
                        cabin="economy",
                        price_type="miles",
                        currency="BRL",
                        price_cash=None,
                        miles=miles,
                        miles_program="azul",
                        taxes=taxes,
                        source="tudoazul",
                        booking_url=azul_url(
                            origin_code,
                            dest_code,
                            dep_day,
                            return_date if kind == "ida" else None,
                        ),
                        departure_time=dep_time,
                        arrival_time=arr_time,
                        duration=duration,
                        fare=fare_name,
                        trip_kind=kind,
                    )
                )
    return offers


def _parse_latam(
    data: dict,
    origin: str,
    dest: str,
    day: str,
    return_date: str | None,
    cheapest: bool = True,
) -> list[Offer]:
    offers: list[Offer] = []
    for item in data.get("items") or data.get("offers") or []:
        price = item.get("price") or {}
        amount = _num(price.get("amount") or price.get("total") or item.get("miles") or item.get("points"))
        if amount is None:
            continue
        flight = item.get("flight") or {}
        route = item.get("route") or {}
        fare = item.get("fare") or {}
        # Resgate LATAM Pass vem em pontos inteiros; tarifa em dinheiro tem centavos ou valor menor.
        is_miles = data.get("redemption") is True or (amount >= 1000 and float(amount).is_integer())
        if not is_miles:
            continue
        offers.append(
            Offer(
                origin=str(route.get("originIata") or origin),
                destination=str(route.get("destinationIata") or dest),
                departure_date=day,
                return_date=return_date,
                airline=str(flight.get("flightCode") or "LATAM"),
                stops=flight.get("stops"),
                cabin="economy" if "econ" in str(fare.get("cabinLabel") or "econ").lower() else "business",
                price_type="miles",
                currency="BRL",
                price_cash=None,
                miles=int(amount),
                miles_program="latam",
                taxes=_num(price.get("taxes") or item.get("taxes")),
                source="latampass",
                booking_url=latam_url(origin, dest, day, True, return_date),
                departure_time=flight.get("departureTime"),
                arrival_time=flight.get("arrivalTime"),
                duration=flight.get("duration"),
                operators=flight.get("operators"),
                layover=flight.get("layover"),
            )
        )
    offers.sort(key=lambda item: (item.miles or 10**9, item.departure_time or ""))
    return _cheapest_only(offers) if cheapest else offers


def _cheapest_only(offers: list[Offer]) -> list[Offer]:
    if not offers:
        return []
    return [min(offers, key=lambda item: item.miles or 10**9)]


async def search_brazilian_miles(
    origins: list[str],
    destinations: list[str],
    start: date,
    end: date,
    trip_type: str,
    stay_nights: int,
    programs: list[str],
    sample_step: int = 7,
) -> list[Offer]:
    if not GECKOAPI_API_KEY:
        return []
    dates = sample_dates(start, end, sample_step)
    offers: list[Offer] = []
    async with httpx.AsyncClient() as client:
        for program in programs:
            if program not in {"smiles", "azul", "latam"}:
                continue
            origs = collapse_rio_codes(origins, program)
            dests = collapse_rio_codes(destinations, program)
            pairs = [
                (origin, dest)
                for origin in origs
                for dest in dests
                if origin != dest
            ]
            for origin, dest in pairs:
                for day in dates:
                    day_s = day.isoformat()
                    return_s = None
                    if trip_type == "round_trip":
                        return_s = (day + timedelta(days=stay_nights)).isoformat()
                    data = await _extract(
                        client,
                        build_extract_body(program, origin, dest, day_s, return_s),
                    )
                    if data:
                        offers.extend(parse_extract(program, data, origin, dest, day_s, return_s))
    return offers
