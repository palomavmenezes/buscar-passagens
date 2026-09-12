from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from app.airports import collapse_rio_codes
from app.config import GECKOAPI_API_KEY
from app.dates import sample_dates
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


def smiles_url(origin: str, dest: str, day: str, return_date: str | None) -> str:
    url = (
        "https://www.smiles.com.br/mfe/emissao-passagem"
        f"?originAirportCode={origin}&destinationAirportCode={dest}&departureDate={day}"
        "&adults=1&children=0&infants=0&isElegible=false"
    )
    if return_date:
        url += f"&returnDate={return_date}"
    return url


def azul_url(origin: str, dest: str, day: str, return_date: str | None = None) -> str:
    pretty = day[5:7] + "%2F" + day[8:10] + "%2F" + day[:4]
    url = (
        "https://www.voeazul.com.br/br/pt/home/selecao-voo"
        f"?c[0].ds={origin}&c[0].as={dest}&c[0].std={pretty}"
        "&p[0].t=ADT&p[0].c=1&p[0].tc=BRL"
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
    if return_date:
        outbound = [item for item in items if not _is_return_segment(item)]
        inbound = [item for item in items if _is_return_segment(item)]
        if outbound and inbound:
            out_vals = [pair for pair in (_smiles_best(item) for item in outbound) if pair]
            in_vals = [pair for pair in (_smiles_best(item) for item in inbound) if pair]
            if out_vals and in_vals:
                out_best = min(out_vals, key=lambda pair: pair[0])
                in_best = min(in_vals, key=lambda pair: pair[0])
                return [
                    Offer(
                        origin=origin,
                        destination=dest,
                        departure_date=day,
                        return_date=return_date,
                        airline="GOL",
                        stops=None,
                        cabin="economy",
                        price_type="miles",
                        currency="BRL",
                        price_cash=None,
                        miles=out_best[0] + in_best[0],
                        miles_program="smiles",
                        taxes=(out_best[1] or 0) + (in_best[1] or 0),
                        source="smiles",
                        booking_url=smiles_url(origin, dest, day, return_date),
                    )
                ]
    offers: list[Offer] = []
    for item in items:
        best = _smiles_best(item)
        if not best:
            continue
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
                origin=origin,
                destination=dest,
                departure_date=day,
                return_date=return_date,
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
                booking_url=smiles_url(origin, dest, day, return_date),
            )
        )
    return _cheapest_only(offers)


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


def _azul_journey_best(journey: dict) -> tuple[int, float | None] | None:
    best = None
    for fare in journey.get("fares") or []:
        for option in fare.get("pointsOptions") or []:
            miles = _miles(option.get("discountedPoints") or option.get("points"))
            if miles is None:
                continue
            money = option.get("taxesAndFees") or option.get("totalMoney") or {}
            taxes = _num(money.get("amount") if isinstance(money, dict) else money)
            if best is None or miles < best[0]:
                best = (miles, taxes)
    return best


def _parse_azul(data: dict, origin: str, dest: str, day: str, return_date: str | None) -> list[Offer]:
    offers: list[Offer] = []
    for trip in data.get("trips") or []:
        journeys = trip.get("journeys") or [trip]
        if return_date and len(journeys) >= 2:
            out_best = _azul_journey_best(journeys[0])
            in_best = _azul_journey_best(journeys[1])
            if out_best and in_best:
                offers.append(
                    Offer(
                        origin=origin,
                        destination=dest,
                        departure_date=day,
                        return_date=return_date,
                        airline="Azul",
                        stops=None,
                        cabin="economy",
                        price_type="miles",
                        currency="BRL",
                        price_cash=None,
                        miles=out_best[0] + in_best[0],
                        miles_program="azul",
                        taxes=(out_best[1] or 0) + (in_best[1] or 0),
                        source="tudoazul",
                        booking_url=azul_url(origin, dest, day),
                    )
                )
                continue
        for journey in journeys:
            best = _azul_journey_best(journey)
            if not best:
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
                origin,
            ) or origin
            dest_code = _azul_iata(
                journey.get("arrivalStation"),
                journey.get("destination"),
                last.get("arrivalStation"),
                last.get("destination"),
                last.get("arrival"),
                dest,
            ) or dest
            offers.append(
                Offer(
                    origin=origin_code,
                    destination=dest_code,
                    departure_date=day,
                    return_date=return_date,
                    airline="Azul",
                    stops=max(0, len(segments) - 1) if segments else None,
                    cabin="economy",
                    price_type="miles",
                    currency="BRL",
                    price_cash=None,
                    miles=best[0],
                    miles_program="azul",
                    taxes=best[1],
                    source="tudoazul",
                    booking_url=azul_url(origin, dest, day),
                )
            )
    return _cheapest_only(offers)


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
                taxes=None,
                source="latampass",
                booking_url=latam_url(origin, dest, day, True, return_date),
                departure_time=flight.get("departureTime"),
                arrival_time=flight.get("arrivalTime"),
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
