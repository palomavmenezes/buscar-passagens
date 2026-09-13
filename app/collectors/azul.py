from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from app.airports import expand_city_airports
from app.collectors.browser import (
    click_load_more_flights,
    collect_home_jobs,
    first_visible,
    open_login,
    pause,
)
from app.config import AZUL_PROFILE_DIR
from app.miles_budget import (
    AZUL_FARE_DIAMANTE,
    AZUL_FARE_PUBLICA,
    AZUL_FARE_UNICA,
    azul_fare_pairs,
    plausible_miles,
)
from app.providers.base import Offer
from app.providers.brazilian import _parse_azul, azul_url

SESSION_MARK = AZUL_PROFILE_DIR / ".logged_in"
AZUL_HOME = "https://www.voeazul.com.br/br/pt/home"
BFF_HINTS = (
    "availability",
    "select-flight",
    "selectflight",
    "flight/search",
    "flights/search",
    "tudoazul",
    "points",
    "ibe/",
)
JSON_KEYS = ("trips", "journeys", "fares", "pointsOptions", "flights", "availability")
BLOCKED = (
    "comportamento incomum",
    "acesso foi limitado",
    "access denied",
    "demorando mais",
)
LOGGED = (
    "saldo de pontos",
    "você está no nível",
    "consultar extrato",
    "crédito azul",
    "sair da conta",
    "encerrar sessão",
)

READ_AZUL_CARDS_JS = r"""(arg) => {
  const originSet = new Set(arg.origins || []);
  const destSet = new Set(arg.dests || []);
  const ptsRe = /(\d{1,3}\.\d{3}\.\d{3}|\d{2,3}\.\d{3}|\d{5,7})\s*pontos/i;
  const toPts = (raw) => {
    const n = parseInt(String(raw).replace(/\./g, ''), 10);
    return (n >= 12000 && n <= 9000000) ? n : null;
  };
  const isStrike = (el) => {
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i++) {
      const tag = (node.tagName || '').toLowerCase();
      if (tag === 's' || tag === 'del') return true;
      const st = window.getComputedStyle(node);
      if ((st.textDecorationLine || '').includes('line-through')) return true;
      node = node.parentElement;
    }
    return false;
  };
  const mixed = (text) => /pontos\s*\+|ou\s+\d[\d.]*\s*pontos\s*\+/i.test(text || '');
  const iataOf = (text, prefer) => {
    const codes = [...String(text || '').matchAll(/\b([A-Z]{3})\b/g)].map((item) => item[1])
      .filter((code) => !['BRL', 'PTS', 'ADT', 'RIO'].includes(code));
    const wanted = new Set(prefer || []);
    return codes.find((code) => wanted.has(code)) || codes[0] || '';
  };
  const buttons = [...document.querySelectorAll('button, a, [role=button]')].filter((el) =>
    /ver tarifas/i.test(el.innerText || el.getAttribute('aria-label') || '')
  );
  const cards = [];
  const seen = new Set();
  for (const btn of buttons) {
    let root = btn;
    for (let i = 0; i < 14 && root.parentElement; i++) {
      root = root.parentElement;
      const t = (root.innerText || '').replace(/\s+/g, ' ');
      if (/ver tarifas/i.test(t) && ptsRe.test(t) && t.length > 50 && t.length < 2800) break;
    }
    const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
    const key = text.slice(0, 140);
    if (seen.has(key)) continue;
    seen.add(key);
    const nodes = [...root.querySelectorAll('s, del, span, div, p, strong, b, em')];
    let normal = null;
    let diamond = null;
    for (const node of nodes) {
      const chunk = (node.innerText || '').replace(/\s+/g, ' ').trim();
      if (chunk.length > 48 || mixed(chunk)) continue;
      const match = chunk.match(ptsRe);
      if (!match) continue;
      const miles = toPts(match[1]);
      if (!miles) continue;
      if (isStrike(node)) normal = miles;
      else diamond = diamond == null ? miles : Math.min(diamond, miles);
    }
    if (normal == null || diamond == null) {
      const listed = [];
      for (const match of text.matchAll(/(\d{1,3}\.\d{3}\.\d{3}|\d{2,3}\.\d{3}|\d{5,7})\s*pontos/gi)) {
        const idx = match.index || 0;
        const around = text.slice(Math.max(0, idx - 8), idx + match[0].length + 12);
        if (mixed(around) || /\+\s*R\$/i.test(around)) continue;
        const miles = toPts(match[1]);
        if (miles) listed.push(miles);
      }
      const uniq = [...new Set(listed)].sort((a, b) => b - a);
      if (uniq.length >= 2) {
        if (normal != null && diamond == null) {
          const cheaper = uniq.find((miles) => miles < normal);
          if (cheaper) diamond = cheaper;
        } else if (diamond != null && normal == null) {
          const higher = uniq.find((miles) => miles > diamond);
          if (higher) normal = higher;
        } else if (normal == null && diamond == null) {
          diamond = uniq[uniq.length - 1];
        }
      } else if (uniq.length === 1) {
        if (normal == null && diamond == null) diamond = uniq[0];
        else if (diamond == null && normal && uniq[0] < normal) diamond = uniq[0];
        else if (normal == null && diamond && uniq[0] > diamond) normal = uniq[0];
      }
    }
    if (diamond != null && normal != null && diamond >= normal) {
      const swap = normal;
      normal = diamond;
      diamond = swap < normal ? swap : diamond;
      if (diamond >= normal) diamond = null;
    }
    const clocks = [...text.matchAll(/\b(\d{1,2}:\d{2})\b/g)].map((item) => item[1]);
    const stopsMatch = text.match(/(\d+)\s*conex/i);
    const direto = /direto|sem conex/i.test(text);
    const durationMatch = text.match(/dura[cç][aã]o:\s*(\d+\s*h(?:\s*\d+\s*m)?|\d+\s*m)/i);
    const originCode = iataOf(text, [...originSet, ...destSet]);
    const destCode = iataOf(text.replace(originCode, ''), [...destSet, ...originSet]);
    const inbound = originSet.has(destCode) || (destSet.has(originCode) && !originSet.has(originCode));
    cards.push({
      origin: originCode,
      destination: destCode,
      departure: clocks[0] || null,
      arrival: clocks[1] || null,
      stops: stopsMatch ? Number(stopsMatch[1]) : (direto ? 0 : null),
      duration: durationMatch ? durationMatch[1].replace(/\s+/g, ' ') : null,
      inbound,
      normal,
      diamond,
    });
  }
  return { ok: cards.length > 0, cards, count: cards.length };
}"""


def azul_session_ready() -> bool:
    return SESSION_MARK.exists() and AZUL_PROFILE_DIR.exists()


def _keep_azul_miles(offers: list[Offer]) -> list[Offer]:
    kept: list[Offer] = []
    skipped = 0
    for offer in offers:
        if plausible_miles(
            offer.miles,
            offer.origin,
            offer.destination,
            offer.cabin,
            program="azul",
            fare=offer.fare,
            trip_kind=offer.trip_kind,
        ):
            kept.append(offer)
        else:
            skipped += 1
    if skipped:
        print(f"Ignoro {skipped} voos Azul acima do teto do trecho.", flush=True)
    return kept


def parse_azul_payload(payload: dict[str, Any], origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    if payload.get("source") == "dom":
        return offers_from_azul_cards(payload.get("cards") or [], origin, dest, day, return_day)
    offers = _parse_azul(payload, origin, dest, day, return_day)
    if not offers:
        wrapped = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if wrapped:
            offers = _parse_azul(wrapped, origin, dest, day, return_day)
    return _keep_azul_miles(offers)


def offers_from_azul_cards(
    cards: list[dict[str, Any]],
    origin: str,
    dest: str,
    day: str,
    return_day: str | None,
) -> list[Offer]:
    offers: list[Offer] = []
    origin_set = {origin.upper(), *expand_city_airports(origin)}
    dest_set = {dest.upper(), *expand_city_airports(dest)}
    skipped = 0
    seen_normal: list[int] = []
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
        normal = int(card["normal"]) if card.get("normal") else None
        diamond = int(card["diamond"]) if card.get("diamond") else None
        if normal:
            seen_normal.append(normal)
        pairs = azul_fare_pairs(normal, diamond)
        kept = []
        for fare_name, miles in pairs:
            if plausible_miles(
                miles,
                origin_code,
                dest_code,
                program="azul",
                fare=fare_name,
                trip_kind=kind,
            ):
                kept.append((fare_name, miles))
        if not kept:
            skipped += 1
            continue
        for fare_name, miles in kept:
            offers.append(
                Offer(
                    origin=origin_code,
                    destination=dest_code,
                    departure_date=dep_day,
                    return_date=return_day if kind == "ida" else None,
                    airline="Azul",
                    stops=card.get("stops"),
                    cabin="economy",
                    price_type="miles",
                    currency="BRL",
                    price_cash=None,
                    miles=miles,
                    miles_program="azul",
                    taxes=None,
                    source="tudoazul",
                    booking_url=azul_url(
                        origin_code,
                        dest_code,
                        dep_day,
                        return_day if kind == "ida" else None,
                    ),
                    departure_time=card.get("departure"),
                    arrival_time=card.get("arrival"),
                    duration=card.get("duration"),
                    fare=fare_name,
                    trip_kind=kind,
                )
            )
    if seen_normal:
        print(
            f"Azul tarifa normal na tela: {min(seen_normal)}–{max(seen_normal)} pontos.",
            flush=True,
        )
    if skipped:
        print(f"Ignoro {skipped} voos Azul acima do teto do trecho.", flush=True)
    return offers


async def read_azul_page(page, origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    extra = await click_load_more_flights(page)
    for label in ("voo de volta", "voos de volta", "volta"):
        heading = await first_visible(page.get_by_text(re.compile(label, re.I)), timeout=800)
        if heading is not None:
            try:
                await heading.scroll_into_view_if_needed()
            except Exception:
                pass
            await pause(page, 0.6, 1.0)
            extra += await click_load_more_flights(page)
            break
    if extra:
        print(f"Carreguei mais {extra} vezes para ver todos os voos da Azul.", flush=True)
    data = await page.evaluate(
        READ_AZUL_CARDS_JS,
        {
            "origins": [origin.upper(), *expand_city_airports(origin)],
            "dests": [dest.upper(), *expand_city_airports(dest)],
        },
    )
    cards = (data or {}).get("cards") or []
    offers = offers_from_azul_cards(cards, origin, dest, day, return_day)
    public = sum(1 for offer in offers if offer.fare == AZUL_FARE_PUBLICA)
    diamonds = sum(1 for offer in offers if offer.fare == AZUL_FARE_DIAMANTE)
    unique = sum(1 for offer in offers if offer.fare == AZUL_FARE_UNICA)
    idas = sum(1 for offer in offers if offer.trip_kind == "ida")
    voltas = sum(1 for offer in offers if offer.trip_kind == "volta")
    print(
        f"Azul na tela: {len(cards)} voos, {idas} idas / {voltas} voltas, "
        f"{public} Tarifa Pública, {diamonds} Tarifa Diamante, {unique} Tarifa. Sem clicar em voo.",
        flush=True,
    )
    return offers


async def collect_azul_jobs(jobs: list[dict[str, Any]], raw_dir: Path, **kwargs) -> list[dict[str, Any]]:
    kwargs.pop("redemption", None)
    if "pause_seconds" not in kwargs and "pause" in kwargs:
        kwargs["pause_seconds"] = kwargs.pop("pause")
    else:
        kwargs.pop("pause", None)
    return await collect_home_jobs(
        jobs,
        raw_dir,
        profile_dir=AZUL_PROFILE_DIR,
        home=AZUL_HOME,
        label="Azul",
        program="azul",
        bff_hints=BFF_HINTS,
        json_keys=JSON_KEYS,
        round_labels=(),
        oneway_labels=("trecho único", "somente ida", "só ida", "one way"),
        origin_labels=("origem", "saindo de", "partida", "aeroporto de origem", "^de$", "origin", "from"),
        dest_labels=("destino", "indo para", "chegando em", "aeroporto de destino", "^para$", "destination", "to"),
        miles_labels=("usar pontos azul", "usar pontos"),
        results_miles_labels=(),
        require_airport_option=True,
        search_labels=("buscar passagens", "buscar voos", "procurar voos"),
        blocked_hints=BLOCKED,
        parse_offers=parse_azul_payload,
        booking_url=lambda origin, dest, day, back: azul_url(origin, dest, day, back),
        miles_after_dates=True,
        read_page=read_azul_page,
        session_mark=SESSION_MARK,
        **kwargs,
    )


async def open_azul_login() -> dict[str, Any]:
    return await open_login(AZUL_PROFILE_DIR, AZUL_HOME, "Azul / TudoAzul", LOGGED, SESSION_MARK)


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "login"
    if command != "login":
        raise SystemExit("Use: python3 -m app.collectors.azul login")
    result = asyncio.run(open_azul_login())
    print("Sessão Azul salva." if result.get("ok") else "Ainda precisa fazer login.", flush=True)
    print(result.get("url") or "", flush=True)
