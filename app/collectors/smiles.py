from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

from app.airports import AIRPORTS, CITY_AIRPORTS, expand_city_airports
from app.collectors.browser import _airport_type_hints, collect_home_jobs, first_visible, pause
from app.collectors.human import human_click, human_type
from app.config import SMILES_GUEST_PROFILE_DIR
from app.miles_budget import SMILES_FARE_CASH, SMILES_FARE_CLIENT, SMILES_FARE_CLUB, smiles_keep_offer
from app.providers.base import Offer
from app.providers.brazilian import _parse_smiles, miles_values_from_json, smiles_url

SMILES_HOME = "https://www.smiles.com.br/home"
BFF_HINTS = (
    "air-flightsearch",
    "airlines/search",
    "flightsearch",
    "smiles/api",
    "/v1/air",
    "/v2/air",
)
JSON_KEYS = ("offers", "requestedFlightSegmentList", "flights", "fareOptions", "smiles")
BLOCKED = (
    "access denied",
    "acesso negado",
    "demorando mais",
    "captcha",
    "unusual traffic",
)
READ_SMILES_CARDS_JS = r"""(arg) => {
  const originSet = new Set(arg.origins || []);
  const destSet = new Set(arg.dests || []);
  const milesRe = /(\d{1,3}(?:\.\d{3})+|\d{3,7})\s*milhas/gi;
  const cashRe = /ou\s*R\$\s*([\d.]+,\d{2})/i;
  const toMiles = (raw) => {
    const n = parseInt(String(raw).replace(/\./g, ''), 10);
    return (n >= 1000 && n <= 9000000) ? n : null;
  };
  const toCash = (raw) => {
    const n = parseFloat(String(raw).replace(/\./g, '').replace(',', '.'));
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  const iataOf = (text, prefer) => {
    const codes = [...String(text || '').matchAll(/\b([A-Z]{3})\b/g)].map((item) => item[1])
      .filter((code) => !['BRL', 'PTS', 'ADT', 'RIO', 'SAO'].includes(code));
    const wanted = new Set(prefer || []);
    return codes.find((code) => wanted.has(code)) || codes[0] || '';
  };
  const buttons = [...document.querySelectorAll('button, a, [role=button]')].filter((el) =>
    /selecionar tarifa|selecionar|escolher voo|escolher este|ver tarifa/i.test(el.innerText || el.getAttribute('aria-label') || '')
  );
  const cards = [];
  const seen = new Set();
  const roots = buttons.length ? buttons.map((btn) => {
    let root = btn;
    for (let i = 0; i < 12 && root.parentElement; i++) {
      root = root.parentElement;
      const t = (root.innerText || '').replace(/\s+/g, ' ');
      if (/milhas|smiles/i.test(t) && t.length > 40 && t.length < 2500) break;
    }
    return root;
  }) : [];
  if (!roots.length) {
    for (const node of document.querySelectorAll('article, li, [class*="flight"], [class*="card"]')) {
      const t = (node.innerText || '').replace(/\s+/g, ' ');
      if (/milhas/i.test(t) && /\d{1,2}:\d{2}/.test(t) && t.length > 40 && t.length < 2500) roots.push(node);
    }
  }
  for (const root of roots) {
    const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
    const key = text.slice(0, 160);
    if (!text || seen.has(key)) continue;
    seen.add(key);
    const listed = [];
    for (const match of text.matchAll(milesRe)) {
      const miles = toMiles(match[1]);
      if (miles) listed.push(miles);
    }
    const uniq = [...new Set(listed)].sort((a, b) => a - b);
    const cashMatch = text.match(cashRe);
    const clocks = [...text.matchAll(/\b(\d{1,2}:\d{2})\b/g)].map((item) => item[1]);
    if (!uniq.length) continue;
    if (!clocks.length && !/selecionar|escolher voo|escolher este|ver tarifa/i.test(text)) continue;
    const stopsMatch = text.match(/(\d+)\s*parada|(\d+)\s*conex/i);
    const direto = /direto|sem parada|sem conex/i.test(text);
    const originCode = iataOf(text, [...originSet, ...destSet]);
    const destCode = iataOf(text.replace(originCode, ''), [...destSet, ...originSet]);
    const inbound = originSet.has(destCode) || (destSet.has(originCode) && !originSet.has(originCode));
    cards.push({
      origin: originCode,
      destination: destCode,
      departure: clocks[0] || null,
      arrival: clocks[1] || null,
      stops: stopsMatch ? Number(stopsMatch[1] || stopsMatch[2]) : (direto ? 0 : null),
      inbound,
      club: /clube smiles/i.test(text),
      miles: uniq[0] || null,
      milesClub: uniq.length > 1 ? uniq[0] : (/clube smiles/i.test(text) ? uniq[0] : null),
      milesClient: uniq.length > 1 ? uniq[uniq.length - 1] : uniq[0] || null,
      cash: cashMatch ? toCash(cashMatch[1]) : null,
    });
  }
  return { cards };
}"""


def smiles_session_ready() -> bool:
    return True


def _keep(offer: Offer) -> bool:
    return smiles_keep_offer(offer.miles, offer.price_cash, offer.origin, offer.destination)


def parse_smiles_payload(payload: dict[str, Any], origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    offers: list[Offer] = []
    if payload.get("source") == "dom" and payload.get("cards"):
        return offers_from_smiles_cards(payload.get("cards") or [], origin, dest, day, return_day)
    if payload.get("source") == "dom" and payload.get("miles"):
        offers.append(
            Offer(
                origin=origin,
                destination=dest,
                departure_date=day,
                return_date=return_day,
                airline="GOL",
                stops=None,
                cabin="economy",
                price_type="miles",
                currency="BRL",
                price_cash=None,
                miles=int(payload["miles"]),
                miles_program="smiles",
                taxes=None,
                source="smiles",
                booking_url=smiles_url(origin, dest, day, return_day),
                fare=SMILES_FARE_CLIENT,
                trip_kind="ida",
            )
        )
    else:
        for candidate in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
            if not candidate:
                continue
            parsed = _parse_smiles(candidate, origin, dest, day, return_day)
            if parsed:
                for offer in parsed:
                    if not offer.fare:
                        offer.fare = SMILES_FARE_CLIENT
                offers.extend(parsed)
                break
        if not offers:
            values = sorted(set(miles_values_from_json(payload)))
            offers.extend(
                Offer(
                    origin=origin,
                    destination=dest,
                    departure_date=day,
                    return_date=return_day,
                    airline="GOL",
                    stops=None,
                    cabin="economy",
                    price_type="miles",
                    currency="BRL",
                    price_cash=None,
                    miles=amount,
                    miles_program="smiles",
                    taxes=None,
                    source="smiles",
                    booking_url=smiles_url(origin, dest, day, return_day),
                    fare=SMILES_FARE_CLIENT,
                    trip_kind="ida",
                )
                for amount in values
            )
    kept = [offer for offer in offers if _keep(offer)]
    print(
        f"GOL/Smiles: {len(offers)} tarifas lidas, {len(kept)} dentro do teto "
        f"(nacional 15 mil / R$ 400; internacional 50 mil / R$ 1.000).",
        flush=True,
    )
    return kept


def offers_from_smiles_cards(
    cards: list[dict[str, Any]],
    origin: str,
    dest: str,
    day: str,
    return_day: str | None,
) -> list[Offer]:
    origin_set = {origin.upper(), *expand_city_airports(origin)}
    dest_set = {dest.upper(), *expand_city_airports(dest)}
    offers: list[Offer] = []
    for card in cards:
        inbound = bool(card.get("inbound"))
        origin_code = str(card.get("origin") or (dest if inbound else origin)).upper()
        dest_code = str(card.get("destination") or (origin if inbound else dest)).upper()
        if origin_code not in origin_set and origin_code not in dest_set:
            origin_code = dest if inbound else origin
        if dest_code not in dest_set and dest_code not in origin_set:
            dest_code = origin if inbound else dest
        kind = "volta" if inbound else "ida"
        dep_day = return_day if inbound and return_day else day
        fares: list[tuple[str, int | None, float | None]] = []
        client = card.get("milesClient") or card.get("miles")
        club = card.get("milesClub")
        cash = card.get("cash")
        if client:
            fares.append((SMILES_FARE_CLIENT, int(client), None))
        if club and club != client:
            fares.append((SMILES_FARE_CLUB, int(club), None))
        if cash:
            fares.append((SMILES_FARE_CASH, None, float(cash)))
        for fare_name, miles, money in fares:
            offer = Offer(
                origin=origin_code,
                destination=dest_code,
                departure_date=dep_day,
                return_date=return_day if kind == "ida" else None,
                airline="GOL",
                stops=card.get("stops"),
                cabin="economy",
                price_type="cash" if fare_name == SMILES_FARE_CASH else "miles",
                currency="BRL",
                price_cash=money,
                miles=miles,
                miles_program="smiles",
                taxes=None,
                source="smiles",
                booking_url=smiles_url(
                    origin_code,
                    dest_code,
                    dep_day,
                    return_day if kind == "ida" else None,
                ),
                departure_time=card.get("departure"),
                arrival_time=card.get("arrival"),
                fare=fare_name,
                trip_kind=kind,
            )
            if _keep(offer):
                offers.append(offer)
    return offers


async def _smiles_cards(page, origin: str, dest: str) -> list[dict[str, Any]]:
    data = await page.evaluate(
        READ_SMILES_CARDS_JS,
        {
            "origins": [origin.upper(), *expand_city_airports(origin)],
            "dests": [dest.upper(), *expand_city_airports(dest)],
        },
    )
    return (data or {}).get("cards") or []


async def _click_named_smiles(page, pattern: re.Pattern[str], timeout: int = 2500):
    locators = [
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.locator("button, a, [role=button], [role=radio], label").filter(has_text=pattern),
    ]
    for locator in locators:
        target = await first_visible(locator, timeout=timeout)
        if target is None:
            continue
        clicked = await human_click(target, page=page)
        if not clicked:
            try:
                await target.click(timeout=2500, force=True)
                clicked = True
            except Exception:
                clicked = False
        if clicked:
            return True
    return False


async def _click_first_smiles_outbound(page) -> bool:
    if await _click_named_smiles(page, re.compile(r"selecionar tarifa", re.I), timeout=1800):
        print("Cliquei em Selecionar tarifa na ida da GOL/Smiles.", flush=True)
        return True
    print("Não achei o botão Selecionar tarifa na ida da GOL.", flush=True)
    return False


async def _wait_smiles_fare_panel(page) -> bool:
    for _ in range(20):
        try:
            text = (await page.inner_text("body") or "").lower()
        except Exception:
            text = ""
        if "use milhas" in text or "confirmar seleção" in text or "confirmar selecao" in text:
            if "tarifa para clientes smiles" in text:
                return True
        await page.wait_for_timeout(250)
    return False


async def _pick_smiles_client_fare(page) -> bool:
    await _wait_smiles_fare_panel(page)
    try:
        picked = await page.evaluate(
            """() => {
              const ok = (t) => /tarifa para clientes smiles/i.test(t)
                && !/clube/i.test(t)
                && !/money/i.test(t)
                && !/combinar/i.test(t);
              const nodes = [...document.querySelectorAll('label, [role="radio"], p, span, div, li, button')];
              let best = null;
              let bestLen = 1e9;
              for (const el of nodes) {
                const box = el.getBoundingClientRect();
                if (box.width < 8 || box.height < 8) continue;
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!ok(t) || t.length > 160) continue;
                if (t.length < bestLen) {
                  best = el;
                  bestLen = t.length;
                }
              }
              if (!best) return false;
              const radio = best.querySelector('input[type="radio"], [role="radio"]')
                || best.closest('label')
                || best.closest('[role="radio"]')
                || best;
              radio.click();
              return true;
            }"""
        )
    except Exception:
        picked = False
    if picked:
        print("Marquei a tarifa para clientes Smiles na ida.", flush=True)
        await pause(page, 0.4, 0.8)
        return True
    print("Não achei a tarifa para clientes Smiles na ida.", flush=True)
    return False


async def _confirm_smiles_selection(page) -> bool:
    pattern = re.compile(r"confirmar seleção|confirmar selecao", re.I)
    for _ in range(12):
        btn = page.get_by_role("button", name=pattern)
        target = await first_visible(btn, timeout=700)
        if target is None:
            extra = page.locator("button").filter(has_text=pattern)
            target = await first_visible(extra, timeout=500)
        if target is not None:
            disabled = (await target.get_attribute("disabled")) is not None
            aria = ((await target.get_attribute("aria-disabled")) or "").lower()
            if not disabled and aria not in {"true", "1"}:
                clicked = await human_click(target, page=page)
                if not clicked:
                    try:
                        await target.click(timeout=2500, force=True)
                        clicked = True
                    except Exception:
                        clicked = False
                if clicked:
                    print("Cliquei em Confirmar seleção na GOL/Smiles.", flush=True)
                    return True
        try:
            clicked = await page.evaluate(
                """() => {
                  const btn = [...document.querySelectorAll('button, [role="button"]')].find((el) => {
                    const t = (el.innerText || el.getAttribute('aria-label') || '');
                    return /confirmar sele[cç][aã]o/i.test(t);
                  });
                  if (!btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true') return false;
                  btn.click();
                  return true;
                }"""
            )
        except Exception:
            clicked = False
        if clicked:
            print("Cliquei em Confirmar seleção na GOL/Smiles.", flush=True)
            return True
        await page.wait_for_timeout(400)
    print("Não cliquei em Confirmar seleção na GOL/Smiles.", flush=True)
    return False


async def _wait_smiles_results(page) -> str:
    last = ""
    saw_loading = False
    after_load = 0
    for step in range(180):
        try:
            last = await page.inner_text("body") or ""
        except Exception:
            last = ""
        low = last.lower()
        loading = "aguarde enquanto buscamos" in low
        hours = len(re.findall(r"\b\d{1,2}:\d{2}\b", last))
        has_fare = bool(re.search(r"tarifa para clientes|tarifa smiles|clube smiles", low))
        empty = "não encontramos" in low or "nao encontramos" in low
        if loading:
            saw_loading = True
            after_load = 0
            if step and step % 20 == 0:
                print("Ainda espero a lista de voos da GOL/Smiles.", flush=True)
            await page.wait_for_timeout(1000)
            continue
        if empty and (saw_loading or step > 8):
            print("A GOL/Smiles não encontrou voos nesta busca.", flush=True)
            return last
        if hours >= 2 and has_fare:
            print("A lista de voos da GOL/Smiles apareceu.", flush=True)
            return last
        if saw_loading and hours >= 2:
            print("A lista de voos da GOL/Smiles apareceu.", flush=True)
            return last
        if saw_loading:
            after_load += 1
            if after_load >= 10 and hours:
                print("A lista de voos da GOL/Smiles apareceu.", flush=True)
                return last
        await page.wait_for_timeout(1000)
    print("Esperei a lista da GOL/Smiles; sigo com o que estiver na tela.", flush=True)
    return last


async def read_smiles_page(page, origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    href = (page.url or "").lower()
    if "emissao-passagem" not in href and "passagens-aereas" not in href:
        print("Ainda na home da GOL/Smiles; não leio a vitrine como voo.", flush=True)
        return []
    await _wait_smiles_results(page)
    cards = await _smiles_cards(page, origin, dest)
    inbound_cards = [card for card in cards if card.get("inbound")]
    outbound_cards = [card for card in cards if not card.get("inbound")]
    if return_day and not inbound_cards:
        print("Clico em Selecionar tarifa, marco a tarifa Smiles e confirmo a ida.", flush=True)
        if await _click_first_smiles_outbound(page):
            await pause(page, 0.8, 1.4)
            picked = await _pick_smiles_client_fare(page)
            confirmed = await _confirm_smiles_selection(page) if picked else False
            if confirmed:
                await pause(page, 1.8, 3.2)
                await _wait_smiles_results(page)
                for _ in range(40):
                    inbound_cards = [
                        card for card in await _smiles_cards(page, origin, dest) if card.get("inbound")
                    ]
                    if inbound_cards:
                        break
                    await page.wait_for_timeout(700)
            else:
                print("A volta da GOL/Smiles não abriu; fico com a ida.", flush=True)
        else:
            print("Fico com a lista da ida; a volta não abriu.", flush=True)
    all_cards = outbound_cards + inbound_cards
    offers = offers_from_smiles_cards(all_cards, origin, dest, day, return_day)
    idas = sum(1 for offer in offers if offer.trip_kind == "ida")
    voltas = sum(1 for offer in offers if offer.trip_kind == "volta")
    print(
        f"GOL na tela: {len(all_cards)} voos, {idas} idas / {voltas} voltas. "
        "Uma busca cobre os dois destinos.",
        flush=True,
    )
    return offers


def _new_smiles_guest_profile() -> Path:
    folder = SMILES_GUEST_PROFILE_DIR / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    print("Abro um Chrome da GOL/Smiles do zero, pasta nova, sem cookies nem login.", flush=True)
    return folder


SMILES_MONTHS = (
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
SMILES_MONTH_ABBR = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def _smiles_day_label(day: str) -> str:
    parsed = date.fromisoformat(day[:10])
    return f"{parsed.day} de {SMILES_MONTHS[parsed.month - 1]} de {parsed.year}"


def _smiles_airport_patterns(code: str) -> list[re.Pattern[str]]:
    typed = code.strip().upper()
    city = str((AIRPORTS.get(typed) or {}).get("city") or "").split("(")[0].strip()
    patterns: list[re.Pattern[str]] = []
    if typed in CITY_AIRPORTS:
        patterns.append(re.compile(r"todos os aeroportos", re.I))
    patterns.append(re.compile(rf"\b{re.escape(typed)}\b", re.I))
    if city:
        patterns.append(re.compile(re.escape(city), re.I))
    return patterns


async def _smiles_visible_suggestion(page, field_id: str, code: str):
    menu = page.locator(f"#{field_id}").locator("xpath=../following-sibling::div[1]").locator("li.list-group-item")
    fallback = page.locator("li.list-group-item")
    for pattern in _smiles_airport_patterns(code):
        for root in (menu, fallback):
            scoped = root.filter(has_text=pattern)
            try:
                await scoped.first.wait_for(state="visible", timeout=4500)
            except Exception:
                continue
            count = await scoped.count()
            for index in range(count):
                item = scoped.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text() or "").lower()
                except Exception:
                    continue
                if "explorar o mundo" in text:
                    continue
                return item
    return None


async def _fill_smiles_airport(page, field_id: str, code: str) -> bool:
    field = page.locator(f"#{field_id}")
    if await first_visible(field, timeout=2500) is None:
        return False
    typed = code.strip().upper()
    await human_click(field, page=page)
    await pause(page, 0.2, 0.4)
    for hint in _airport_type_hints(typed):
        print(f"Digito {hint} no campo da GOL/Smiles.", flush=True)
        await human_type(page, field, hint)
        target = await _smiles_visible_suggestion(page, field_id, typed)
        if target is None:
            print(f"A lista da GOL/Smiles não mostrou {typed} depois de {hint}.", flush=True)
            continue
        clicked = await human_click(target, page=page)
        if not clicked:
            try:
                await target.click(timeout=2500, force=True)
                clicked = True
            except Exception:
                clicked = False
        if clicked:
            print(f"Selecionei {typed} na lista da GOL/Smiles.", flush=True)
            await pause(page, 0.35, 0.7)
            return True
        print(f"A lista da GOL/Smiles não mostrou {typed} depois de {hint}.", flush=True)
    return False


async def _dismiss_smiles_popups(page) -> None:
    for locator in (
        page.get_by_role("button", name=re.compile(r"^fechar$|^close$", re.I)),
        page.locator('[aria-label="Fechar"], [aria-label="Close"]'),
    ):
        try:
            if await locator.count() and await locator.first.is_visible():
                await locator.first.click(timeout=800)
                await pause(page, 0.15, 0.35)
                break
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _smiles_field_has_day(locator, day: str) -> bool:
    try:
        value = (await locator.input_value() or "").casefold()
    except Exception:
        value = ""
    parsed = date.fromisoformat(day[:10])
    abbr = SMILES_MONTH_ABBR[parsed.month - 1]
    padded = f"{parsed.day:02d}"
    bare = str(parsed.day)
    has_day = padded in value or re.search(rf"(^|\D){bare}(\D|$)", value)
    return bool(has_day and abbr in value)


async def _smiles_calendar_open(page) -> bool:
    try:
        await page.wait_for_function(
            """() => [...document.querySelectorAll('.CalendarDay')].some((el) => {
              const box = el.getBoundingClientRect();
              return box.width > 0 && box.height > 0 && box.bottom > 0 && box.top < window.innerHeight;
            })""",
            timeout=4000,
        )
        return True
    except Exception:
        return False


async def _visible_smiles_calendar_day(page, day: str):
    label = _smiles_day_label(day)
    cell = page.locator(f'.CalendarDay[aria-label*="{label}"]')
    for index in range(await cell.count()):
        item = cell.nth(index)
        try:
            if not await item.is_visible():
                continue
            cls = (await item.get_attribute("class")) or ""
        except Exception:
            continue
        if "blocked" in cls:
            continue
        return item
    return None


async def _first_visible_day_label(page) -> str:
    days = page.locator(".CalendarDay")
    for index in range(min(await days.count(), 80)):
        item = days.nth(index)
        try:
            if await item.is_visible():
                return (await item.get_attribute("aria-label")) or ""
        except Exception:
            continue
    return ""


async def _click_smiles_next_month(page) -> bool:
    locators = [
        page.locator("#btn_nextCalendar"),
        page.locator("button.calendar-navigation.button-right"),
        page.locator('[aria-label*="next month" i]'),
        page.locator('[aria-label*="Move forward" i]'),
        page.locator("button.DayPickerNavigation_rightButton__horizontal"),
        page.locator("button").filter(has_text=re.compile(r"^navigate_next$")),
    ]
    before = await _first_visible_day_label(page)
    for locator in locators:
        try:
            if await locator.count() == 0:
                continue
            btn = locator.last
            try:
                await btn.click(timeout=1500)
            except Exception:
                await btn.click(timeout=1500, force=True)
            await pause(page, 0.4, 0.7)
            after = await _first_visible_day_label(page)
            if after and after != before:
                return True
        except Exception:
            continue
    return False


async def _click_smiles_calendar_day(page, day: str) -> bool:
    label = _smiles_day_label(day)
    for _ in range(24):
        target = await _visible_smiles_calendar_day(page, day)
        if target is not None:
            try:
                await target.click(timeout=2000)
            except Exception:
                try:
                    await target.click(timeout=2000, force=True)
                except Exception:
                    target = None
            if target is not None:
                await pause(page, 0.35, 0.6)
                return True
        if not await _click_smiles_next_month(page):
            break
    print(f"Não achei {label} no calendário da GOL/Smiles.", flush=True)
    return False


async def _pick_smiles_date(page, field_id: str, day: str) -> bool:
    field = page.locator(f"#{field_id}")
    if await _smiles_field_has_day(field, day):
        return True
    if not await _smiles_calendar_open(page):
        await human_click(field, page=page)
        await pause(page, 0.45, 0.8)
    if not await _smiles_calendar_open(page):
        return False
    if not await _click_smiles_calendar_day(page, day):
        return False
    if await _smiles_field_has_day(field, day):
        return True
    print(f"O campo de data da GOL/Smiles não ficou com {_smiles_day_label(day)}.", flush=True)
    return False


async def fill_smiles_search(page, origin: str, dest: str, day: str, return_day: str | None) -> bool:
    print("Preencho o buscador da home da GOL/Smiles, não o formulário da Azul/LATAM.", flush=True)
    await _dismiss_smiles_popups(page)
    trip = page.locator("#drop_fligthType")
    trip_text = ""
    try:
        if await first_visible(trip, timeout=2500) is not None:
            trip_text = (await trip.inner_text() or "").lower()
    except Exception:
        trip_text = ""
    want_round = bool(return_day)
    already = ("ida e volta" in trip_text) if want_round else ("somente ida" in trip_text)
    if already:
        print("Trecho: ida e volta." if want_round else "Trecho: somente ida.", flush=True)
    elif await first_visible(trip, timeout=800) is not None:
        await human_click(trip, page=page)
        await pause(page, 0.25, 0.5)
        opt = page.locator("#opt_roundTrip" if want_round else "#opt_oneWay")
        if await first_visible(opt, timeout=1200) is not None:
            await human_click(opt, page=page)
            print("Trecho: ida e volta." if want_round else "Trecho: somente ida.", flush=True)
        else:
            await page.keyboard.press("Escape")
    if not await _fill_smiles_airport(page, "inp_flightOrigin_1", origin):
        print("Não selecionei a origem na GOL/Smiles.", flush=True)
        return False
    await pause(page, 0.4, 0.8)
    if not await _fill_smiles_airport(page, "inp_flightDestination_1", dest):
        print("Não selecionei o destino na GOL/Smiles.", flush=True)
        return False
    await pause(page, 0.4, 0.8)
    start = page.locator("#startDateId")
    if await first_visible(start, timeout=2000) is None:
        print("Não achei o campo de ida da GOL/Smiles.", flush=True)
        return False
    await human_click(start, page=page)
    await pause(page, 0.5, 0.9)
    if not await _smiles_calendar_open(page):
        try:
            await start.click(timeout=1500, force=True)
            await pause(page, 0.5, 0.9)
        except Exception:
            pass
    if not await _smiles_calendar_open(page):
        icon = page.locator(".DateRangePickerInput_calendarIcon").first
        if await first_visible(icon, timeout=800) is not None:
            await human_click(icon, page=page)
            await pause(page, 0.5, 0.9)
    if not await _smiles_calendar_open(page):
        print("O calendário da GOL/Smiles não abriu.", flush=True)
        return False
    print("Calendário da GOL/Smiles aberto.", flush=True)
    if not await _pick_smiles_date(page, "startDateId", day):
        print("Não preenchi a ida no calendário da GOL/Smiles.", flush=True)
        return False
    if return_day:
        end = page.locator("#endDateId")
        if not await _smiles_field_has_day(end, return_day):
            if not await _smiles_calendar_open(page):
                await human_click(end, page=page)
                await pause(page, 0.35, 0.7)
            if not await _pick_smiles_date(page, "endDateId", return_day):
                print("Não preenchi a volta no calendário da GOL/Smiles.", flush=True)
                return False
    start_val = ""
    end_val = ""
    try:
        start_val = await start.input_value()
        if return_day:
            end_val = await page.locator("#endDateId").input_value()
    except Exception:
        start_val = ""
    print(f"Datas na GOL/Smiles: {start_val}" + (f" / {end_val}" if return_day else ""), flush=True)
    search = page.locator("#btn_search")
    if await first_visible(search, timeout=2000) is None:
        print("Não achei o botão Buscar voos da GOL/Smiles.", flush=True)
        return False
    await human_click(search, page=page)
    print("Cliquei em Buscar voos na home da GOL/Smiles.", flush=True)
    try:
        await page.wait_for_url(re.compile(r"emissao-passagem|passagens-aereas"), timeout=25000)
    except Exception:
        if await first_visible(page.get_by_text(re.compile(r"insira as datas", re.I)), timeout=800):
            print("A GOL/Smiles ainda pede as datas da viagem.", flush=True)
            return False
        href = (page.url or "").lower()
        if "emissao-passagem" not in href and "passagens-aereas" not in href:
            print("A busca da GOL/Smiles não saiu da home.", flush=True)
            return False
    return True


async def collect_smiles_jobs(jobs: list[dict[str, Any]], raw_dir: Path, **kwargs) -> list[dict[str, Any]]:
    kwargs.pop("redemption", None)
    kwargs.pop("session_mark", None)
    kwargs.pop("halt_on", None)
    kwargs.pop("fresh_session", None)
    if "pause_seconds" not in kwargs and "pause" in kwargs:
        kwargs["pause_seconds"] = kwargs.pop("pause")
    else:
        kwargs.pop("pause", None)
    kwargs["session_mark"] = None
    kwargs["halt_on"] = {"denied"}
    kwargs.setdefault("wait_loops", 180)
    kwargs.setdefault("startup_wait_seconds", 30)
    kwargs["fill_search"] = fill_smiles_search
    return await collect_home_jobs(
        jobs,
        raw_dir,
        profile_dir=_new_smiles_guest_profile(),
        home=SMILES_HOME,
        label="GOL/Smiles",
        program="smiles",
        bff_hints=BFF_HINTS,
        json_keys=JSON_KEYS,
        round_labels=("ida e volta", "round trip"),
        oneway_labels=("somente ida", "só ida", "one way"),
        origin_labels=("origem", "^de$", "origin", "from"),
        dest_labels=("destino", "^para$", "destination", "to"),
        miles_labels=(),
        search_labels=("buscar voos", "buscar", "pesquisar", "procurar voos"),
        blocked_hints=BLOCKED,
        parse_offers=parse_smiles_payload,
        booking_url=lambda origin, dest, day, back: smiles_url(origin, dest, day, back),
        read_page=read_smiles_page,
        **kwargs,
    )


async def open_smiles_login() -> dict[str, Any]:
    print(
        "A GOL/Smiles não precisa de login para tarifas de milhas e dinheiro. "
        "A coleta usa um Chrome visitante, sem a sua conta.",
        flush=True,
    )
    return {"ok": True, "url": SMILES_HOME, "guest": True}


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "login"
    if command != "login":
        raise SystemExit("Use: python3 -m app.collectors.smiles login")
    result = asyncio.run(open_smiles_login())
    print(result.get("url") or "", flush=True)
