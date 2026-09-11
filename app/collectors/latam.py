from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, LATAM_HEADLESS, LATAM_PAUSE_SECONDS, LATAM_PROFILE_DIR
from app.airports import expand_city_airports
from app.providers.base import Offer
from app.providers.brazilian import _parse_latam, latam_url

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DATA_DIR / "playwright-browsers"))

SESSION_MARK = LATAM_PROFILE_DIR / ".logged_in"
LATAM_HOME = "https://www.latamairlines.com/br/pt"
BFF_HINTS = ("/bff/air-offers/v2/offers/search", "air-offers/offers/search")
LOGIN_HINTS = (
    "faça seu login",
    "faca seu login",
    "inicie sessão",
    "iniciar sessão",
    "entre ou cadastre",
)
LOGGED_HINTS = ("olá,", "ola,", "encerrar sessão", "cerrar sesión")
MILES_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})+|\d{4,7})\s*(?:milhas|pts|points|pontos)",
    re.I,
)


def latam_session_ready() -> bool:
    return SESSION_MARK.exists() and LATAM_PROFILE_DIR.exists()


def _save_session() -> None:
    LATAM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_MARK.write_text("ok\n", encoding="utf-8")


def _looks_logged(state: str, body: str, cookies: list[dict[str, Any]] | None = None) -> bool:
    if state == "login":
        return False
    if any(hint in body for hint in LOGIN_HINTS):
        return False
    if state == "offers" or any(hint in body for hint in LOGGED_HINTS):
        return True
    names = " ".join(str(item.get("name") or "").lower() for item in cookies or [])
    hosts = " ".join(str(item.get("domain") or "") for item in cookies or [])
    if "latamairlines.com" not in hosts and "latam.com" not in hosts:
        return False
    return any(token in names for token in ("token", "auth", "session", "id_token", "access"))


def _chromium_args() -> list[str]:
    # Sem sandbox porque este Linux bloqueia user namespace; não é bypass da LATAM.
    return [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ]


async def _launch_context(playwright, headless: bool | None = None):
    LATAM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(LATAM_PROFILE_DIR),
        channel="chrome",
        headless=LATAM_HEADLESS if headless is None else headless,
        args=_chromium_args(),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 900},
    )


async def latam_page_state(page) -> str:
    href = (page.url or "").lower()
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = ""
    if "auth.latamairlines.com" in href or "/u/login" in href:
        return "login"
    if "application error" in text or "client-side exception" in text:
        return "app_error"
    if "access denied" in text or "no pudimos permitir" in text or "motivos de seguridad" in text:
        return "denied"
    if "oferta-voos" in href:
        return "offers"
    return "other"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")


def parse_clock(value: Any) -> str | None:
    if value in (None, "", False):
        return None
    if isinstance(value, dict):
        return parse_clock(
            value.get("dateTime")
            or value.get("localDateTime")
            or value.get("time")
            or value.get("departure")
            or value.get("arrival")
            or value.get("scheduled")
        )
    text = str(value).strip()
    if "T" in text:
        clock = text.split("T", 1)[1][:5]
        if CLOCK_RE.match(clock):
            hour, minute = clock.split(":")
            return f"{int(hour):02d}:{minute}"
    match = CLOCK_RE.search(text)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def summary_times(summary: dict[str, Any]) -> tuple[str | None, str | None]:
    origin = summary.get("origin") if isinstance(summary.get("origin"), dict) else {}
    dest = summary.get("destination") if isinstance(summary.get("destination"), dict) else {}
    departure = parse_clock(
        origin.get("departure")
        or origin.get("departureDateTime")
        or origin.get("departureTime")
        or origin.get("dateTime")
        or summary.get("departure")
        or summary.get("departureDateTime")
        or summary.get("departureTime")
    )
    arrival = parse_clock(
        dest.get("arrival")
        or dest.get("arrivalDateTime")
        or dest.get("arrivalTime")
        or dest.get("dateTime")
        or summary.get("arrival")
        or summary.get("arrivalDateTime")
        or summary.get("arrivalTime")
    )
    if departure and arrival:
        return departure, arrival
    itinerary = summary.get("itinerary") or summary.get("flightItinerary") or {}
    segments = []
    if isinstance(itinerary, dict):
        segments = itinerary.get("segments") or itinerary.get("legs") or []
    if not segments:
        segments = summary.get("segments") or summary.get("legs") or []
    if isinstance(segments, list) and segments:
        first = segments[0] if isinstance(segments[0], dict) else {}
        last = segments[-1] if isinstance(segments[-1], dict) else {}
        departure = departure or parse_clock(first.get("departure") or first.get("origin"))
        arrival = arrival or parse_clock(last.get("arrival") or last.get("destination"))
    return departure, arrival


def _looks_like_miles(node: dict[str, Any], amount: float) -> bool:
    currency = str(node.get("currency") or node.get("currencyCode") or node.get("code") or "").upper()
    display = str(node.get("displayCurrency") or node.get("display") or node.get("srLabel") or "").lower()
    if currency in {"POINTS", "MILES", "PTS", "MILHAS", "POINT", "LOYALTY_POINTS"}:
        return amount >= 500
    if "milha" in display or "pts" in display or "points" in display:
        return amount >= 500
    return False


def bff_to_gecko(payload: dict[str, Any], origin: str, dest: str, day: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary") or {}
            if not isinstance(summary, dict):
                continue
            best_amount = None
            cabin = "econ"
            for brand in summary.get("brands") or []:
                if not isinstance(brand, dict):
                    continue
                price = brand.get("price") or {}
                if str(price.get("currency") or "").upper() != "LOYALTY_POINTS":
                    continue
                amount = _as_number(price.get("amount"))
                if amount is None or amount < 500:
                    continue
                if best_amount is None or amount < best_amount:
                    best_amount = amount
                    cabin = str((brand.get("cabin") or {}).get("label") or brand.get("brandText") or "econ")
            if best_amount is None:
                continue
            departure, arrival = summary_times(summary)
            items.append(
                {
                    "route": {
                        "originIata": summary.get("origin", {}).get("iataCode") or origin,
                        "destinationIata": summary.get("destination", {}).get("iataCode") or dest,
                    },
                    "flight": {
                        "flightCode": summary.get("flightCode") or "LATAM",
                        "stops": summary.get("stopOvers"),
                        "departureTime": departure,
                        "arrivalTime": arrival,
                    },
                    "fare": {"cabinLabel": cabin},
                    "price": {"amount": int(best_amount), "currency": "POINTS", "total": int(best_amount)},
                }
            )
        if items:
            return {"redemption": True, "items": items, "source": "latam-local"}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        price = node.get("price")
        if isinstance(price, dict) and str(price.get("currency") or "").upper() == "LOYALTY_POINTS":
            amount = _as_number(price.get("amount"))
            if amount is not None and amount >= 500:
                items.append(
                    {
                        "route": {"originIata": origin, "destinationIata": dest},
                        "flight": {
                            "flightCode": node.get("flightCode") or "LATAM",
                            "stops": node.get("stopOvers"),
                            "departureTime": parse_clock(node.get("departure") or node.get("origin")),
                            "arrivalTime": parse_clock(node.get("arrival") or node.get("destination")),
                        },
                        "fare": {"cabinLabel": str(node.get("cabin") or "econ")},
                        "price": {"amount": int(amount), "currency": "POINTS", "total": int(amount)},
                    }
                )
        for key, child in node.items():
            if key in {"lowestPriceDifference", "taxes", "priceWithOutTax"}:
                continue
            if isinstance(child, (dict, list)):
                walk(child)

    walk(payload)
    return {"redemption": True, "items": items, "source": "latam-local"}


def offers_from_latam_bff(
    payload: dict[str, Any],
    origin: str,
    dest: str,
    day: str,
    kind: str,
    return_date: str | None = None,
) -> list[Offer]:
    if kind != "miles":
        return []
    return _parse_latam(bff_to_gecko(payload, origin, dest, day), origin, dest, day, return_date, cheapest=False)


def _url_has_place(url: str, code: str) -> bool:
    token = (code or "").strip().lower()
    if not token:
        return True
    aliases = {token, *[item.lower() for item in expand_city_airports(code)]}
    return any(alias in url for alias in aliases)


def miles_from_text(text: str) -> int | None:
    best = None
    for match in MILES_RE.finditer(text or ""):
        raw = match.group(1).replace(".", "")
        try:
            value = int(raw)
        except ValueError:
            continue
        if 1000 <= value <= 9_000_000 and (best is None or value < best):
            best = value
    return best


def _browser_closed(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "has been closed" in message or "targetclosed" in message or "target closed" in message


async def _safe_goto(page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        if _browser_closed(exc):
            raise
        message = str(exc)
        if "ERR_ABORTED" in message or "interrupted" in message.lower():
            await page.wait_for_timeout(4000)
            return
        raise


async def _pause(page, low: float = 0.4, high: float = 1.1) -> None:
    await page.wait_for_timeout(int(random.uniform(low, high) * 1000))


async def _first_visible(locator, timeout: int = 2000):
    try:
        count = await locator.count()
    except Exception:
        return None
    if count == 0:
        return None
    for index in range(min(count, 12)):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=350):
                return item
        except Exception:
            continue
    try:
        await locator.first.wait_for(state="visible", timeout=timeout)
        return locator.first
    except Exception:
        return None


async def _click_named(root, labels: tuple[str, ...], roles: tuple[str, ...] = ("button", "radio", "tab", "checkbox")):
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in roles:
            target = await _first_visible(root.get_by_role(role, name=pattern), timeout=700)
            if target is not None:
                await target.click()
                return label
    return None


async def _dismiss_banners(page) -> None:
    await _click_named(page, ("aceitar todos", "aceitar cookies", "^aceitar$", "^aceito$", "concordo"))
    try:
        cookie = page.locator("#onetrust-accept-btn-handler")
        if await cookie.count() and await cookie.first.is_visible(timeout=800):
            await cookie.first.click()
    except Exception:
        pass


async def _search_roots(page):
    yield page
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        url = (frame.url or "").lower()
        if not url or url == "about:blank" or "onetrust" in url or "cookie" in url:
            continue
        yield frame


async def _find_field(root, labels: tuple[str, ...], extra=None):
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in ("combobox", "textbox", "searchbox", "button"):
            field = await _first_visible(root.get_by_role(role, name=pattern), timeout=800)
            if field is not None:
                return field
        field = await _first_visible(root.get_by_placeholder(pattern), timeout=600)
        if field is not None:
            return field
        field = await _first_visible(root.get_by_label(pattern), timeout=600)
        if field is not None:
            return field
    if extra is not None:
        field = await _first_visible(extra, timeout=800)
        if field is not None:
            return field
    return None


async def _type_slowly(page, field, text: str) -> None:
    await field.click()
    await _pause(page, 0.25, 0.55)
    try:
        await field.fill("")
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    await _pause(page, 0.2, 0.45)
    try:
        await field.press_sequentially(text, delay=random.randint(90, 170))
    except Exception:
        await page.keyboard.type(text, delay=random.randint(90, 170))


async def _choose_airport(page, root, labels: tuple[str, ...], code: str, extra=None, slot: int = 0) -> bool:
    field = await _find_field(root, labels, extra=extra)
    if field is None:
        cities = root.get_by_placeholder(re.compile(r"cidade ou aeroporto", re.I))
        try:
            if await cities.count() > slot:
                field = await _first_visible(cities.nth(slot), timeout=800)
        except Exception:
            field = None
    if field is None:
        return False
    await _type_slowly(page, field, code)
    await _pause(page, 0.7, 1.4)
    option = await _first_visible(
        root.get_by_role("option").filter(has_text=re.compile(rf"\b{code}\b", re.I)),
        timeout=2500,
    )
    if option is None:
        option = await _first_visible(page.get_by_role("option").filter(has_text=re.compile(rf"\b{code}\b", re.I)), timeout=1200)
    if option is not None:
        await option.click()
        return True
    await page.keyboard.press("ArrowDown")
    await _pause(page, 0.2, 0.4)
    await page.keyboard.press("Enter")
    return True


async def _pick_date(page, root, day: str) -> bool:
    parsed = date.fromisoformat(day)
    shown = parsed.strftime("%d/%m/%Y")
    months = (
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    )
    field = await _find_field(
        root,
        ("data de ida", "data da ida", "data de partida", "partida"),
        extra=root.get_by_placeholder(re.compile(r"dd/mm|aaaa", re.I)),
    )
    if field is None:
        field = await _first_visible(
            root.get_by_role("button", name=re.compile(r"^ida$|data de ida|escolher data", re.I)),
            timeout=900,
        )
    if field is not None:
        await field.click()
        await _pause(page, 0.35, 0.8)
        try:
            focused = await _first_visible(root.locator("input:focus"), timeout=500)
            typer = focused or field
            await typer.fill("")
            await typer.press_sequentially(shown, delay=random.randint(70, 130))
            await page.keyboard.press("Enter")
            await _pause(page, 0.3, 0.6)
            return True
        except Exception:
            pass
    wanted = f"{months[parsed.month - 1]} {parsed.year}"
    for _ in range(18):
        body = ""
        try:
            body = (await root.inner_text("body")).lower()
        except Exception:
            pass
        if wanted in body:
            break
        next_btn = await _first_visible(
            root.get_by_role("button", name=re.compile(r"próximo|proximo|next|seguinte|mês seguinte", re.I)),
            timeout=500,
        )
        if next_btn is None:
            break
        await next_btn.click()
        await _pause(page, 0.25, 0.5)
    day_btn = await _first_visible(
        root.get_by_role("gridcell", name=re.compile(rf"\b{parsed.day}\b")).or_(
            root.get_by_role("button", name=re.compile(rf"^{parsed.day}$"))
        ),
        timeout=1500,
    )
    if day_btn is None:
        print("Não achei o dia no calendário da home.", flush=True)
        return False
    await day_btn.click()
    await _pause(page, 0.25, 0.5)
    await _click_named(root, ("aplicar", "confirmar", "ok", "pronto"))
    return True


async def _ensure_miles(root) -> bool:
    pattern = re.compile(r"usar milhas|buscar com milhas|usar pontos|latam pass|resgatar", re.I)
    box = await _first_visible(root.get_by_role("checkbox", name=pattern), timeout=1000)
    if box is None:
        box = await _first_visible(root.get_by_role("switch", name=pattern), timeout=700)
    if box is not None:
        try:
            if await box.is_checked():
                return True
        except Exception:
            pass
        await box.click()
        return True
    clicked = await _click_named(root, ("usar milhas", "buscar com milhas", "usar pontos latam pass"))
    return bool(clicked)


async def _debug_controls(page) -> None:
    for role in ("combobox", "textbox", "button", "checkbox", "radio", "tab"):
        locator = page.get_by_role(role)
        try:
            count = min(await locator.count(), 15)
        except Exception:
            continue
        names: list[str] = []
        for index in range(count):
            try:
                label = (await locator.nth(index).inner_text()).strip().replace("\n", " ")
            except Exception:
                label = ""
            if label:
                names.append(label[:40])
        if names:
            print(f"  {role}: {names}", flush=True)


async def _fill_airport_field(page, field, code: str) -> bool:
    try:
        if not await field.count():
            return False
        await field.wait_for(state="visible", timeout=4000)
    except Exception:
        return False
    await _type_slowly(page, field, code)
    await _pause(page, 0.7, 1.4)
    if code.upper() == "RIO":
        option = await _first_visible(
            page.get_by_role("option").filter(has_text=re.compile(r"todos os aeroportos|\bRIO\b", re.I)),
            timeout=2500,
        )
    else:
        option = await _first_visible(
            page.get_by_role("option").filter(has_text=re.compile(rf"\b{code}\b", re.I)),
            timeout=2500,
        )
    if option is not None:
        await option.click()
        return True
    await page.keyboard.press("ArrowDown")
    await _pause(page, 0.2, 0.4)
    await page.keyboard.press("Enter")
    return True


async def _click_calendar_day(page, day: str) -> bool:
    day_btn = page.locator(f"#date-{day}")
    for _ in range(16):
        try:
            if await day_btn.is_visible(timeout=700):
                await day_btn.click()
                await _pause(page, 0.35, 0.7)
                return True
        except Exception:
            pass
        next_btn = await _first_visible(
            page.get_by_role("button", name=re.compile(r"próximo mês|mes seguinte|next month|ir para o próximo", re.I)),
            timeout=400,
        )
        if next_btn is None:
            next_btn = await _first_visible(
                page.locator("[data-testid*='next-month' i], [aria-label*='próximo mês' i], [aria-label*='next month' i]"),
                timeout=400,
            )
        if next_btn is None:
            break
        await next_btn.click()
        await _pause(page, 0.3, 0.55)
    return False


async def _pick_priced_date(page, day: str, field_sel: str = "#fsb-departure--text-field", close: bool = True) -> bool:
    field = page.locator(field_sel)
    if await field.count():
        await field.click()
        await _pause(page, 0.5, 0.9)
        if await _click_calendar_day(page, day):
            if close:
                try:
                    await page.locator(f"#date-{day}").wait_for(state="hidden", timeout=2500)
                except Exception:
                    await page.keyboard.press("Escape")
                    await _pause(page, 0.2, 0.4)
            return True
    return await _pick_date(page, page, day)


async def _pick_round_trip_dates(page, outbound: str, inbound: str) -> bool:
    if not await _pick_priced_date(page, outbound, "#fsb-departure--text-field", close=False):
        return False
    print(f"Ida {outbound} preenchida.", flush=True)
    await _pause(page, 0.35, 0.7)
    if await _click_calendar_day(page, inbound):
        print(f"Volta {inbound} preenchida.", flush=True)
        return True
    if await _pick_priced_date(page, inbound, "#fsb-return--text-field", close=True):
        print(f"Volta {inbound} preenchida.", flush=True)
        return True
    print(f"Não achei a volta {inbound} no calendário.", flush=True)
    return False


async def _mark_checkbox(page, locator) -> bool:
    if not await locator.count():
        return False
    try:
        if await locator.is_checked():
            return True
    except Exception:
        pass
    label = page.locator(f"label[for='{await locator.get_attribute('id') or ''}']") if await locator.get_attribute("id") else None
    targets = []
    if label is not None and await label.count():
        targets.append(label)
    targets.append(locator.locator("xpath=.."))
    targets.append(locator)
    for target in targets:
        try:
            await target.scroll_into_view_if_needed()
            await _pause(page, 0.15, 0.35)
            await target.click(force=True)
            return True
        except Exception:
            continue
    try:
        await locator.evaluate(
            """el => {
                el.checked = true;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        return True
    except Exception:
        return False


async def _is_checked(locator) -> bool:
    try:
        return bool(await locator.is_checked())
    except Exception:
        return False


async def _mark_miles_plus_cash(page) -> bool:
    plus = re.compile(r"usar milhas \+ dinheiro|milhas \+ dinheiro|milhas e dinheiro", re.I)
    plus_box = await _first_visible(page.get_by_role("checkbox", name=plus), timeout=1500)
    redemption = page.locator("#fsb-redemption--input")
    if plus_box is not None:
        if not await _is_checked(plus_box):
            await _mark_checkbox(page, plus_box)
        print("Usar milhas + dinheiro marcado.", flush=True)
        try:
            red_id = await redemption.get_attribute("id") if await redemption.count() else ""
            plus_id = await plus_box.get_attribute("id")
            if red_id and plus_id and red_id != plus_id and not await _is_checked(redemption):
                await _mark_checkbox(page, redemption)
        except Exception:
            pass
        return True
    if await redemption.count() and await _is_checked(redemption):
        print("Usar milhas já estava marcado; não vou clicar de novo para não desligar.", flush=True)
        return True
    marked = await _mark_checkbox(page, redemption) or await _ensure_miles(page)
    print("Usar milhas + dinheiro marcado." if marked else "Não achei Usar milhas + dinheiro.", flush=True)
    return marked


async def _search_from_home(page, origin: str, dest: str, day: str, return_day: str | None = None) -> bool:
    try:
        voos = page.locator("#id-tab-flight")
        if await voos.count():
            await voos.click()
            await _pause(page, 0.35, 0.7)
        origin_field = page.locator("#fsb-origin--text-field")
        try:
            form_visible = await origin_field.is_visible(timeout=1500)
        except Exception:
            form_visible = False
        if not form_visible:
            toggle = page.locator("#fsb-form-toggle")
            if await toggle.count():
                await toggle.click()
                await _pause(page, 0.4, 0.8)
        if return_day:
            trip = await _click_named(page, ("^ida e volta$", "round trip", "ida y vuelta"), roles=("button", "radio", "tab"))
            print(f"Trecho: {trip or 'ida e volta (já selecionado)'}.", flush=True)
        else:
            trip = await _click_named(page, ("somente ida", "só ida", "solo ida", "one way"), roles=("button", "radio", "tab"))
            print(f"Trecho: {trip or 'não achei Somente ida'}.", flush=True)
        await _pause(page, 0.45, 0.95)
        if not await _fill_airport_field(page, origin_field, origin):
            print("Não achei o campo de origem.", flush=True)
            await _debug_controls(page)
            return False
        print(f"Origem {origin} preenchida.", flush=True)
        await _pause(page, 0.55, 1.1)
        dest_field = page.locator("#fsb-destination--text-field")
        if not await _fill_airport_field(page, dest_field, dest):
            print("Não achei o campo de destino.", flush=True)
            await _debug_controls(page)
            return False
        print(f"Destino {dest} preenchido.", flush=True)
        await _pause(page, 0.45, 0.95)
        if return_day:
            if not await _pick_round_trip_dates(page, day, return_day):
                print("Não consegui preencher ida e volta.", flush=True)
                await _debug_controls(page)
                return False
        elif not await _pick_priced_date(page, day):
            print("Não consegui preencher a data.", flush=True)
            await _debug_controls(page)
            return False
        else:
            print(f"Data {day} preenchida.", flush=True)
        await _pause(page, 0.4, 0.8)
        await _mark_miles_plus_cash(page)
        await _pause(page, 0.5, 1.0)
        open_day = page.locator("[id^='date-']").first
        try:
            if await open_day.is_visible(timeout=500):
                await page.keyboard.press("Escape")
                await _pause(page, 0.25, 0.5)
        except Exception:
            pass
        search = page.locator("#fsb-search-flights")
        clicked = False
        if await search.count():
            aria = (await search.get_attribute("aria-label") or "").lower()
            if "deve preencher" in aria or "sem campos" in aria:
                print("O botão ainda pede campos; a data pode não ter fechado o calendário.", flush=True)
            try:
                await search.click(timeout=8000)
                clicked = True
            except Exception:
                await page.keyboard.press("Escape")
                await _pause(page, 0.3, 0.6)
                named = await _click_named(page, ("procurar voos", "buscar voos", "pesquisar voos"))
                clicked = bool(named)
        else:
            named = await _click_named(page, ("procurar voos", "buscar voos", "pesquisar voos", "search flights"))
            clicked = bool(named)
        if not clicked:
            print("Não achei o botão de buscar.", flush=True)
            await _debug_controls(page)
            return False
        print("Cliquei em procurar voos.", flush=True)
        try:
            await page.wait_for_url(re.compile(r"oferta-voos"), timeout=25000)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"Falha ao preencher a home ({exc}).", flush=True)
        await _debug_controls(page)
        return False


async def confirm_latam_session() -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        context = await _launch_context(playwright, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await _safe_goto(page, LATAM_HOME)
            await page.wait_for_timeout(5000)
            state = await latam_page_state(page)
            href = page.url
            body = ""
            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                pass
            cookies = await context.cookies()
            if state == "login" or any(hint in body for hint in LOGIN_HINTS):
                SESSION_MARK.unlink(missing_ok=True)
                await context.close()
                return {"ok": False, "url": href, "error": "login_required", "state": state}
            if _looks_logged(state, body, cookies):
                _save_session()
                await context.close()
                return {"ok": True, "url": href, "state": state}
            await context.close()
            return {"ok": False, "url": href, "error": "unknown", "state": state}
        except Exception as exc:
            try:
                await context.close()
            except Exception:
                pass
            if _browser_closed(exc):
                return {"ok": False, "url": "", "error": "browser_closed"}
            raise


async def open_latam_login() -> dict[str, Any]:
    from playwright.async_api import async_playwright

    probe = latam_url("GIG", "SSA", "2026-10-12", True)
    async with async_playwright() as playwright:
        context = await _launch_context(playwright, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await _safe_goto(page, LATAM_HOME)
        except Exception as exc:
            if _browser_closed(exc):
                print("A janela do Chrome foi fechada antes de gravar a sessão.", flush=True)
                return {"ok": False, "url": "", "error": "browser_closed"}
            raise
        print("Faça login na LATAM Pass nesta janela do Google Chrome. A janela fica aberta até 12 minutos.", flush=True)
        deadline = asyncio.get_event_loop().time() + 12 * 60
        href = page.url
        ok = False
        state = await latam_page_state(page)
        last_probe = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            try:
                state = await latam_page_state(page)
                href = page.url
                body = ""
                try:
                    body = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                cookies = await context.cookies()
                if state == "denied":
                    print("A LATAM bloqueou o acesso (Access Denied). Recarregando a página inicial.", flush=True)
                    await _safe_goto(page, LATAM_HOME)
                    await page.wait_for_timeout(4000)
                    continue
                if state == "app_error":
                    print("A busca da LATAM quebrou no navegador. Voltando à página inicial para você entrar.", flush=True)
                    await _safe_goto(page, LATAM_HOME)
                    await page.wait_for_timeout(4000)
                    continue
                if state == "login" or any(hint in body for hint in LOGIN_HINTS):
                    await page.wait_for_timeout(2000)
                    continue
                if _looks_logged(state, body, cookies):
                    _save_session()
                    ok = True
                    print("Sessão LATAM reconhecida na página inicial.", flush=True)
                    break
                now = asyncio.get_event_loop().time()
                if now - last_probe > 20:
                    last_probe = now
                    await _safe_goto(page, probe)
                    await page.wait_for_timeout(6000)
                    state = await latam_page_state(page)
                    href = page.url
                    body = ""
                    try:
                        body = (await page.inner_text("body")).lower()
                    except Exception:
                        pass
                    cookies = await context.cookies()
                    if state == "offers" or _looks_logged(state, body, cookies):
                        _save_session()
                        ok = True
                        break
                    if state in {"app_error", "denied"}:
                        print("Ainda não deu para abrir a busca. Voltando à home; continue o login se precisar.", flush=True)
                        await _safe_goto(page, LATAM_HOME)
                await page.wait_for_timeout(2000)
            except Exception as exc:
                if _browser_closed(exc):
                    if ok:
                        print("Janela fechada, mas a sessão já estava gravada.", flush=True)
                        return {"ok": True, "url": href, "error": "browser_closed"}
                    print("A janela do Chrome foi fechada antes de gravar a sessão.", flush=True)
                    return {"ok": False, "url": href, "error": "browser_closed"}
                print(f"Falha temporária no navegador ({exc}). Recarregando a home.", flush=True)
                try:
                    await _safe_goto(page, LATAM_HOME)
                    await page.wait_for_timeout(4000)
                except Exception as inner:
                    if _browser_closed(inner):
                        if ok:
                            return {"ok": True, "url": href, "error": "browser_closed"}
                        print("A janela do Chrome foi fechada antes de gravar a sessão.", flush=True)
                        return {"ok": False, "url": href, "error": "browser_closed"}
                    raise
        await context.close()
        return {"ok": ok, "url": href, "state": state}


async def collect_latam_jobs(
    jobs: list[dict[str, Any]],
    raw_dir: Path,
    pause: float | None = None,
    redemption: bool = True,
    halt_on: set[str] | None = None,
    save_raw: bool = True,
    wait_loops: int = 25,
    on_result=None,
) -> list[dict[str, Any]]:
    from playwright.async_api import async_playwright

    wait = LATAM_PAUSE_SECONDS if pause is None else pause
    stop = halt_on if halt_on is not None else {"login", "denied"}
    kind = "miles" if redemption else "cash"
    raw_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        context = await _launch_context(playwright)
        page = context.pages[0] if context.pages else await context.new_page()
        captured: dict[str, Any] = {}
        wanted: dict[str, str] = {"origin": "", "dest": "", "day": ""}

        async def on_response(response) -> None:
            url = response.url.lower()
            if not any(hint in url for hint in BFF_HINTS):
                return
            origin = wanted["origin"].lower()
            dest = wanted["dest"].lower()
            day = wanted["day"]
            if not _url_has_place(url, wanted["origin"]):
                return
            if not _url_has_place(url, wanted["dest"]):
                return
            if day and day not in url and day.replace("-", "") not in url:
                return
            captured.setdefault("urls", []).append(f"{response.status} {response.url[:240]}")
            if response.status != 200:
                captured["bff_error"] = f"status={response.status}"
                return
            try:
                body = await response.json()
            except Exception:
                captured["bff_error"] = f"status={response.status} json_failed"
                return
            if isinstance(body, dict) and ("content" in body or "items" in body or "offers" in body):
                captured["bff"] = body
                captured["bff_url"] = response.url
                captured["bff_status"] = response.status

        page.on("response", on_response)

        for index, job in enumerate(jobs):
            captured.clear()
            origin, dest, day = job["origin"], job["destination"], job["day"]
            return_day = job.get("return_day") or job.get("return_date")
            wanted.update({"origin": origin, "dest": dest, "day": day})
            url = latam_url(origin, dest, day, redemption, return_date=return_day)
            status = "ok"
            payload: dict[str, Any] = {}
            offers: list[Offer] = []
            try:
                captured.clear()
                volta = f" / volta {return_day}" if return_day else ""
                rio_note = " (RIO cobre GIG e SDU)" if origin == "RIO" or dest == "RIO" else ""
                print(f"Home LATAM: {origin} → {dest} {day}{volta}{rio_note}", flush=True)
                await _safe_goto(page, LATAM_HOME)
                await _dismiss_banners(page)
                await _pause(page, 1.8, 3.2)
                filled = await _search_from_home(page, origin, dest, day, return_day=return_day)
                if not filled:
                    status = "error"
                    payload = {"error": "form_fill_failed", "url": page.url}
                    text = ""
                else:
                    text = ""
                    for _ in range(wait_loops):
                        if isinstance(captured.get("bff"), dict):
                            break
                        try:
                            text = await page.inner_text("body")
                        except Exception:
                            text = ""
                        low = text.lower()
                        if miles_from_text(text) or "não encontramos" in low or "nao encontramos" in low:
                            break
                        if "application error" in low or "client-side exception" in low:
                            break
                        if "demorando mais" in low or "tente novamente" in low:
                            break
                        await page.wait_for_timeout(1000)
                    blocked = "403" in " ".join(captured.get("urls") or []) or "demorando mais" in (text or "").lower()
                    state = await latam_page_state(page)
                    href = page.url
                    payload["url"] = href
                    payload["network"] = captured.get("urls") or []
                    if captured.get("bff_url"):
                        payload["bff_url"] = captured.get("bff_url")
                        payload["bff_status"] = captured.get("bff_status")
                    if state == "login" and not captured.get("bff"):
                        status = "login"
                        SESSION_MARK.unlink(missing_ok=True)
                        payload["error"] = "login_required"
                    elif isinstance(captured.get("bff"), dict):
                        offers = offers_from_latam_bff(captured["bff"], origin, dest, day, kind, return_date=return_day)
                        payload["bff_items"] = len((captured["bff"] or {}).get("content") or [])
                        if not offers:
                            status = "empty"
                    elif blocked:
                        status = "throttled"
                        payload["error"] = "throttled"
                    elif state == "denied":
                        status = "denied"
                        payload["error"] = "denied"
                    elif state == "app_error":
                        text = ""
                        try:
                            text = await page.inner_text("body")
                        except Exception:
                            pass
                        miles = miles_from_text(text)
                        payload["dom_text"] = text[:4000]
                        if miles:
                            gecko = {
                                "redemption": True,
                                "items": [
                                    {
                                        "route": {"originIata": origin, "destinationIata": dest},
                                        "flight": {"flightCode": "LATAM"},
                                        "fare": {"cabinLabel": "econ"},
                                        "price": {"amount": miles, "currency": "POINTS", "total": miles},
                                    }
                                ],
                            }
                            offers = _parse_latam(gecko, origin, dest, day, return_day)
                            payload["normalized"] = gecko
                        else:
                            status = "app_error"
                            payload["error"] = "app_error"
                    else:
                        gecko = {"redemption": True, "items": []}
                        text = await page.inner_text("body")
                        miles = miles_from_text(text)
                        payload["dom_text"] = text[:4000]
                        if miles:
                            gecko["items"] = [
                                {
                                    "route": {"originIata": origin, "destinationIata": dest},
                                    "flight": {"flightCode": "LATAM"},
                                    "fare": {"cabinLabel": "econ"},
                                    "price": {"amount": miles, "currency": "POINTS", "total": miles},
                                }
                            ]
                        offers = _parse_latam(gecko, origin, dest, day, return_day)
                        payload["normalized"] = gecko
                        if not offers:
                            status = "empty"
            except Exception as exc:
                status = "error"
                payload = {"error": str(exc)}
            compact = [
                {
                    "origin": offer.origin,
                    "destination": offer.destination,
                    "departure_date": offer.departure_date,
                    "return_date": offer.return_date,
                    "departure_time": offer.departure_time,
                    "arrival_time": offer.arrival_time,
                    "airline": offer.airline,
                    "stops": offer.stops,
                    "milhas": offer.miles,
                    "program": offer.miles_program,
                    "source": offer.source,
                    "booking_url": offer.booking_url,
                }
                for offer in offers
            ]
            if save_raw:
                slim = dict(payload)
                slim.pop("bff", None)
                slim.pop("dom_text", None)
                file_path = raw_dir / (
                    f"{kind}-{origin}-{dest}-{day}-{return_day}.json" if return_day else f"{kind}-{origin}-{dest}-{day}.json"
                )
                keep_old = False
                if not compact and file_path.exists():
                    try:
                        previous = json.loads(file_path.read_text(encoding="utf-8"))
                        keep_old = bool(previous.get("offers"))
                    except (OSError, json.JSONDecodeError):
                        keep_old = False
                if not keep_old:
                    file_path.write_text(
                        json.dumps(
                            {"job": job, "status": status, "url": url, "offers": compact, "payload": slim},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
            else:
                file_path = None
            result = {
                "job": job,
                "status": 200 if status == "ok" else status,
                "payload": {k: v for k, v in payload.items() if k != "bff"},
                "offers": offers,
                "file": str(file_path) if file_path else None,
            }
            results.append(result)
            if on_result:
                on_result(result)
            if status == "throttled":
                print("LATAM limitou as buscas. Encerrando esta leva para não insistir. O que já salvou permanece.", flush=True)
                break
            if status == "error" and (payload.get("error") or "") == "form_fill_failed":
                print("Não consegui preencher a busca na home. Parando para não insistir.", flush=True)
                break
            elif status in stop:
                break
            if index < len(jobs) - 1 and status in {"ok", "empty"}:
                try:
                    await _safe_goto(page, LATAM_HOME)
                except Exception:
                    pass
                jitter = wait * (0.75 + random.random() * 0.5)
                await page.wait_for_timeout(int(jitter * 1000))
        await context.close()
    return results


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command in {"login", "check"}:
        result = asyncio.run(confirm_latam_session() if command == "check" else open_latam_login())
        print(
            "Sessão LATAM salva."
            if result.get("ok")
            else f"Ainda precisa fazer login. ({result.get('error') or 'incompleto'})",
            flush=True,
        )
        print(result.get("url") or "", flush=True)
    else:
        raise SystemExit("Use: python3 -m app.collectors.latam check|login")
