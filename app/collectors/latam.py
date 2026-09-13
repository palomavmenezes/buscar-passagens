from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import (
    CHROME_CDP,
    DATA_DIR,
    LATAM_HEADLESS,
    LATAM_PAUSE_SECONDS,
    LATAM_PROFILE_DIR,
    harvest_ok_route_path,
    harvest_route_path,
)
from app.collectors.browser import _latam_login_gate_visible, login_with_env
from app.collectors.human import (
    browse_results,
    host_page,
    human_click,
    human_pause,
    human_type,
    idle_on_page,
    irregular_region_seconds,
    rest_like_a_person,
    wander_mouse,
)
from app.airports import CITY_AIRPORTS, CITY_MEMBER_CODES, city_search_note, expand_city_airports, iata_from_card
from app.miles_budget import plausible_miles
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


def _use_latam_cdp() -> bool:
    return os.getenv("LATAM_USE_CDP", "0") == "1" and bool(CHROME_CDP)


def _use_fresh_latam_browser() -> bool:
    return os.getenv("LATAM_FRESH_BROWSER", "0") == "1"


def _chromium_args(incognito: bool = False) -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]
    if incognito:
        args.append("--incognito")
    if os.name != "nt":
        args.extend(["--disable-dev-shm-usage", "--no-sandbox"])
    return args


async def _launch_context(playwright, headless: bool | None = None):
    await asyncio.to_thread(_kill_latam_debug_chrome)
    reuse = os.getenv("LATAM_REUSE_FRESH", "0") == "1"
    latest = _latest_fresh_profile() if reuse else None
    if latest is not None:
        profile = latest
        incognito = False
        print("Reabro o Chrome da LATAM no perfil da sessão que já entrou.", flush=True)
    elif _use_fresh_latam_browser():
        profile = _new_latam_profile()
        incognito = True
        print("Abro um Chrome anônimo da LATAM, sem cookies nem dados antigos.", flush=True)
    else:
        profile = LATAM_PROFILE_DIR
        incognito = False
        print("Abro o Chrome da LATAM como uma janela normal, com o perfil logado.", flush=True)
    profile.mkdir(parents=True, exist_ok=True)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="chrome",
        headless=LATAM_HEADLESS if headless is None else headless,
        args=_chromium_args(incognito=incognito),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        color_scheme="light",
        ignore_default_args=["--enable-automation", "--disable-extensions"],
        no_viewport=True,
    )


def _pick_latam_page(context):
    pages = [page for page in context.pages if (page.url or "").startswith("http")]
    for page in pages:
        href = page.url or ""
        if "latamairlines.com" in href and "oferta-voos" in href:
            return page
    for page in pages:
        if "latamairlines.com" in (page.url or ""):
            return page
    return pages[0] if pages else None


def _url_matches_job(href: str, origin: str, dest: str, day: str, return_day: str | None = None) -> bool:
    low = (href or "").lower()
    if "oferta-voos" not in low:
        return False
    if f"origin={origin.lower()}" not in low:
        return False
    if f"destination={dest.lower()}" not in low:
        return False
    if day and day not in low:
        return False
    if return_day and return_day not in low:
        return False
    return True


class LatamHttp2Error(RuntimeError):
    """O Chrome da LATAM caiu com HTTP/2; precisa de janela nova."""


def _looks_http2(text: str) -> bool:
    blob = text or ""
    low = blob.lower()
    return "err_http2" in low or "temporariamente indispon" in low


async def _page_shows_http2(page) -> bool:
    href = page.url or ""
    if href.startswith("chrome-error://") or "chrome-error" in href.lower():
        return True
    try:
        text = await page.inner_text("body")
    except Exception:
        text = ""
    return _looks_http2(f"{href}\n{text}")


def _cdp_base() -> str:
    return (CHROME_CDP or "http://127.0.0.1:9222").rstrip("/")


def _cdp_port() -> int:
    parsed = urlparse(_cdp_base() if "://" in _cdp_base() else f"http://{_cdp_base()}")
    return parsed.port or 9222


def _chrome_exe() -> str:
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(path).exists():
            return path
    found = shutil.which("chrome") or shutil.which("google-chrome")
    return found or "chrome"


def _all_chrome_processes() -> list[tuple[int, str]]:
    try:
        out = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            text=True,
            errors="replace",
        )
    except Exception:
        return []
    data = json.loads(out) if out.strip() else []
    if isinstance(data, dict):
        data = [data]
    items: list[tuple[int, str]] = []
    for item in data:
        cmd = item.get("CommandLine") or ""
        pid = item.get("ProcessId")
        if pid:
            items.append((int(pid), cmd))
    return items


def _is_latam_debug_chrome(cmd: str) -> bool:
    low = (cmd or "").lower()
    if "azul-chrome-profile" in low or "smiles-chrome-profile" in low:
        return False
    if "latam-chrome-profile" in low or "latam-chrome-fresh" in low or "chrome-debug-viannas" in low:
        return True
    port = _cdp_port()
    return f"--remote-debugging-port={port}" in cmd


def _latam_debug_profile(cmd: str | None = None) -> Path:
    text = cmd or ""
    match = re.search(r"--user-data-dir(?:=|\s+)(\"[^\"]+\"|\S+)", text, re.I)
    if match:
        return Path(match.group(1).strip('"'))
    return LATAM_PROFILE_DIR


def _cdp_is_up(timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(f"{_cdp_base()}/json/version", timeout=timeout).read()
        return True
    except Exception:
        return False


def _clear_chrome_locks(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
        path = profile / name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _kill_latam_debug_chrome() -> None:
    pids = [pid for pid, cmd in _all_chrome_processes() if _is_latam_debug_chrome(cmd)]
    mains = [pid for pid, cmd in _all_chrome_processes() if _is_latam_debug_chrome(cmd) and "--type=" not in cmd]
    for pid in mains or pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        print(f"Encerrei o Chrome da LATAM (pid {pid}).", flush=True)
    leftover = [pid for pid, cmd in _all_chrome_processes() if _is_latam_debug_chrome(cmd)]
    for pid in leftover:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        still = any(_is_latam_debug_chrome(cmd) for _, cmd in _all_chrome_processes())
        if not still and not _cdp_is_up(timeout=1.0):
            break
        time.sleep(0.4)
    time.sleep(2.0)
    _clear_chrome_locks(LATAM_PROFILE_DIR)
    fresh_root = Path(os.getenv("TEMP") or str(DATA_DIR)) / "latam-chrome-fresh"
    if fresh_root.exists():
        for child in fresh_root.iterdir():
            _clear_chrome_locks(child)


def _fresh_root() -> Path:
    return Path(os.getenv("TEMP") or str(DATA_DIR)) / "latam-chrome-fresh"


def _latest_fresh_profile() -> Path | None:
    root = _fresh_root()
    if not root.exists():
        return None
    dirs = [path for path in root.iterdir() if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def _latam_session_profile() -> Path:
    if os.getenv("LATAM_REUSE_FRESH", "0") == "1":
        latest = _latest_fresh_profile()
        if latest is not None:
            return latest
    if _use_fresh_latam_browser():
        return _new_latam_profile()
    return LATAM_PROFILE_DIR


def _new_latam_profile() -> Path:
    dest = _fresh_root() / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _launch_latam_debug_chrome(profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    _clear_chrome_locks(profile)
    port = _cdp_port()
    subprocess.Popen(
        [
            _chrome_exe(),
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--start-maximized",
            LATAM_HOME,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if _cdp_is_up(timeout=2.0):
            time.sleep(5.0)
            print(f"Abri um Chrome novo da LATAM na porta {port}, com o perfil logado.", flush=True)
            return
        time.sleep(0.5)
    raise RuntimeError(f"O Chrome novo da LATAM não abriu a porta {port}.")


def _ensure_standalone_latam_chrome(profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    if _cdp_is_up():
        print("O Chrome da LATAM já está aberto. Mantenho a sessão.", flush=True)
        return
    _launch_latam_debug_chrome(profile)
    print("Abro o Chrome da LATAM e deixo a sessão aberta na home.", flush=True)


def _restart_latam_debug_chrome() -> None:
    _kill_latam_debug_chrome()
    _launch_latam_debug_chrome(_latam_session_profile())


async def _fresh_latam_page(context):
    page = await context.new_page()
    for extra in list(context.pages):
        if extra == page:
            continue
        try:
            await extra.close()
        except Exception:
            pass
    await page.wait_for_timeout(4000)
    return page


async def _recycle_latam_browser(playwright, context, on_response=None):
    print("HTTP/2 na LATAM. Troco a janela, mas reuso o mesmo perfil da sessão.", flush=True)
    try:
        await context.close()
    except Exception:
        pass
    await asyncio.to_thread(_restart_latam_debug_chrome)
    context, page, attached = await _connect_or_launch(playwright, fresh=True)
    if on_response is not None:
        try:
            context.on("response", on_response)
        except Exception:
            page.on("response", on_response)
    try:
        await page.goto(LATAM_HOME, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        if _looks_http2(str(exc)):
            raise LatamHttp2Error(str(exc).splitlines()[0])
        raise
    await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK)
    return context, page, attached


def _close_stale_cdp_tabs() -> None:
    """Abas da LATAM presas no carregamento fazem o Playwright travar no CDP."""
    if not _cdp_is_up():
        return
    base = _cdp_base()
    try:
        tabs = json.loads(urllib.request.urlopen(f"{base}/json/list", timeout=5).read())
    except Exception as exc:
        print(f"Não listei as abas do Chrome de debug: {exc}", flush=True)
        return
    pages = [tab for tab in tabs if tab.get("type") == "page"]
    keep: set[str] = set()
    for tab in pages:
        href = tab.get("url") or ""
        if "latamairlines.com" in href and "oferta-voos" in href:
            keep.add(tab["id"])
    if not keep:
        for tab in pages:
            if "latamairlines.com" in (tab.get("url") or ""):
                keep.add(tab["id"])
                break
    if not keep and pages:
        keep.add(pages[0]["id"])
    for tab in pages:
        if tab["id"] in keep:
            continue
        try:
            urllib.request.urlopen(f"{base}/json/close/{tab['id']}", timeout=5).read()
            print(f"Fechei aba extra do Chrome de debug: {(tab.get('url') or '')[:90]}", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass


async def _park_latam_home(page) -> None:
    await _dismiss_banners(page)
    await _safe_goto(page, LATAM_HOME)
    await _dismiss_banners(page)
    print("Chrome na home da LATAM, sessão aberta.", flush=True)


async def _connect_or_launch(playwright, fresh: bool = False):
    profile = _latam_session_profile()
    if fresh:
        await asyncio.to_thread(_kill_latam_debug_chrome)
    await asyncio.to_thread(_ensure_standalone_latam_chrome, profile)
    last_error: Exception | None = None
    for _attempt in range(3):
        if not fresh:
            _close_stale_cdp_tabs()
        try:
            browser = await playwright.chromium.connect_over_cdp(_cdp_base(), timeout=60_000)
            break
        except Exception as exc:
            last_error = exc
            print(f"Não conectei no Chrome da LATAM ({exc}). Tento de novo.", flush=True)
            await asyncio.sleep(2)
            await asyncio.to_thread(_ensure_standalone_latam_chrome, profile)
    else:
        raise last_error or RuntimeError("Não conectei no Chrome da LATAM")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = None if fresh else _pick_latam_page(context)
    href = (page.url or "") if page else ""
    if page is None or href.startswith("about:") or "chrome-error" in href:
        page = await _fresh_latam_page(context)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    print(f"Uso o Chrome da LATAM que fica aberto. Aba: {page.url}", flush=True)
    return context, page, True


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
        if 5000 <= value <= 9_000_000 and (best is None or value < best):
            best = value
    return best


def _parse_brl(text: str) -> float | None:
    match = re.search(r"(?:BRL|R\$)\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})", text or "", re.I)
    if not match:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


def _parse_pt_miles(text: str) -> int | None:
    match = re.search(r"(\d{1,3}(?:\.\d{3})+|\d{4,7})\s*milhas", text or "", re.I)
    if not match:
        return None
    value = int(match.group(1).replace(".", ""))
    return value if 5000 <= value <= 9_000_000 else None


def _clock_from_block(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2}:\d{2})\b", text or "")
    return match.group(1) if match else None


READ_CARDS_JS = r"""() => {
  if (!location.hostname.includes('latamairlines.com')) {
    return { ok: false, reason: 'not-latam', href: location.href, cards: [] };
  }
  const heading = document.body.innerText || '';
  const leg = /voo de volta|vuelo de vuelta|return flight/i.test(heading)
    ? 'volta'
    : (/voo de ida|vuelo de ida|outbound/i.test(heading) ? 'ida' : 'unknown');
  const cards = [...document.querySelectorAll('[data-testid^="wrapper-card-flight-"]')];
  const parsed = [];
  for (const card of cards) {
    const testid = card.getAttribute('data-testid') || '';
    const idx = Number(testid.replace('wrapper-card-flight-', ''));
    const amountText = (card.querySelector('[data-testid$="-amount"]') || {}).innerText || '';
    const mileMatch = amountText.match(/(\d{1,3}(?:\.\d{3})+|\d{4,7})\s*milhas/i);
    if (!mileMatch) continue;
    const miles = parseInt(mileMatch[1].replace(/\./g, ''), 10);
    if (!(miles >= 5000 && miles <= 9000000)) continue;
    const taxMatch = amountText.match(/(?:BRL|R\$)\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})/i);
    const taxes = taxMatch ? parseFloat(taxMatch[1].replace(/\./g, '').replace(',', '.')) : null;
    const duration = ((card.querySelector('[data-testid$="-duration"]') || {}).innerText || '')
      .replace(/dura[cç][aã]o/ig, '').trim();
    const originText = (card.querySelector('[data-testid$="-origin"]') || {}).innerText || '';
    const destText = (card.querySelector('[data-testid$="-destination"]') || {}).innerText || '';
    const footer = (card.querySelector('[data-testid^="footer-card-"]') || {}).innerText || '';
    const iataOf = (text) => {
      const codes = [...String(text || '').matchAll(/\b([A-Z]{3})\b/g)].map((item) => item[1]);
      const CITY = new Set(['RIO', 'SAO']);
      const REAL = new Set(['GIG', 'SDU', 'CGH', 'GRU', 'VCP']);
      return codes.find((code) => REAL.has(code))
        || codes.find((code) => code !== 'BRL' && !CITY.has(code))
        || '';
    };
    const operators = [...card.querySelectorAll('img[data-testid^="image-"]')]
      .map((img) => (img.getAttribute('data-testid') || '').replace(/^image-/, ''))
      .filter((name, i, all) => name && all.indexOf(name) === i);
    const stopsMatch = footer.match(/(\d+)\s*parada/i);
    const direto = /direto|sem parada/i.test(footer);
    parsed.push({
      index: idx,
      miles,
      taxes,
      duration,
      origin: originText.replace(/\s+/g, ' ').trim(),
      destination: destText.replace(/\s+/g, ' ').trim(),
      originIata: iataOf(originText),
      destinationIata: iataOf(destText),
      stops: stopsMatch ? Number(stopsMatch[1]) : (direto ? 0 : null),
      operators,
    });
  }
  parsed.sort((a, b) => a.miles - b.miles || (a.taxes || 0) - (b.taxes || 0));
  return { ok: parsed.length > 0, leg, href: location.href, cheapest: parsed[0] || null, cards: parsed };
}"""

ITINERARY_JS = r"""() => {
  const dialog = document.querySelector('[data-testid="itineraryModal"]')
    || document.querySelector('[role="dialog"]');
  if (!dialog) return null;
  const text = dialog.innerText || '';
  const layovers = [...text.matchAll(/Troca de avi[aã]o em ([^\n]+)\n([^\n]+)/gi)]
    .map((item) => `${item[1].trim()}: ${item[2].trim()}`);
  const flights = [...text.matchAll(/\b([A-Z]{2}\s?\d{3,4})\b\s*\nOperado por ([^\n]+)/g)]
    .map((item) => ({ number: item[1].replace(/\s+/g, ''), operator: item[2].trim() }));
  return { text: text.slice(0, 4000), layovers, flights };
}"""


async def _ensure_latam_page(context, page, quiet: bool = False):
    if await _page_shows_http2(page):
        raise LatamHttp2Error(page.url or "ERR_HTTP2_PROTOCOL_ERROR")
    href = page.url or ""
    if "latamairlines.com" in href and "booking.com" not in href:
        return page
    if not quiet:
        print("A aba saiu da LATAM. Volto para a tela de voos.", flush=True)
    other = _pick_latam_page(context)
    if other:
        try:
            await other.bring_to_front()
        except Exception:
            pass
        return other
    try:
        await page.go_back()
    except Exception:
        pass
    return page


async def _close_latam_dialogs(page) -> None:
    if await _latam_login_gate_visible(page):
        return
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
    except Exception:
        pass
    for selector in (
        '[data-testid$="--dialog-close-button"]',
        '[data-testid="close-brand-list--button"]',
        '[data-testid="boreal-backdrop"]',
        '[aria-label="Fechar"]',
        'button[aria-label="Close"]',
    ):
        locator = page.locator(selector)
        try:
            if await locator.count() and await locator.first.is_visible(timeout=800):
                await locator.first.click(timeout=1500, force=True)
                await page.wait_for_timeout(400)
        except Exception:
            pass


async def _read_flight_cards(page) -> dict[str, Any]:
    try:
        return await page.evaluate(READ_CARDS_JS)
    except Exception:
        return {"ok": False, "cards": [], "reason": "evaluate_failed"}


async def _read_itinerary(page, index: int) -> dict[str, Any]:
    await _close_latam_dialogs(page)
    link = page.locator(f'[data-testid="itinerary-modal-{index}-details-anchor--link"]')
    try:
        if await link.count():
            await link.click(timeout=6000)
            await page.wait_for_timeout(800)
    except Exception:
        pass
    try:
        data = await page.evaluate(ITINERARY_JS)
    except Exception:
        data = None
    await _close_latam_dialogs(page)
    return data or {}


BRANDS_JS = r"""(index) => {
  const root = document.querySelector(`[data-testid="wrapper-card-flight-${index}"]`) || document;
  const nodes = [...root.querySelectorAll('[data-testid*="-price-"]')];
  const brands = [];
  for (const el of nodes) {
    const testid = el.getAttribute('data-testid') || '';
    if (!testid.includes('-price-')) continue;
    const brand = testid.split('-price-')[1] || '';
    if (!brand || brand.length < 3) continue;
    const text = el.innerText || '';
    const mileMatch = text.match(/(\d{1,3}(?:\.\d{3})+|\d{4,7})\s*milhas/i);
    if (!mileMatch) continue;
    const miles = parseInt(mileMatch[1].replace(/\./g, ''), 10);
    const taxMatch = text.match(/(?:BRL|R\$)\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})/i);
    const taxes = taxMatch ? parseFloat(taxMatch[1].replace(/\./g, '').replace(',', '.')) : null;
    brands.push({ brand, miles, taxes, text: text.replace(/\s+/g, ' ').trim().slice(0, 160) });
  }
  return brands;
}"""


async def _expand_card(page, index: int) -> None:
    await _close_latam_dialogs(page)
    header = page.locator(f'[data-testid="wrapper-card-header-{index}"]')
    await human_click(header, page=page, timeout=8000)
    await human_pause(page, 1.4, 2.8)


async def _collapse_card(page, index: int) -> None:
    card = page.locator(f'[data-testid="wrapper-card-flight-{index}"]')
    try:
        if await card.locator('[data-testid^="bundle-detail-"]').count() == 0:
            return
        header = page.locator(f'[data-testid="wrapper-card-header-{index}"]')
        await human_click(header, page=page, timeout=3000)
        await human_pause(page, 0.5, 1.1)
    except Exception:
        pass


async def _read_card_brands(page, index: int) -> list[dict[str, Any]]:
    await _expand_card(page, index)
    try:
        return await page.evaluate(BRANDS_JS, index)
    except Exception:
        return []
    finally:
        await _collapse_card(page, index)


def _cheapest_business_brand(brands: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = [item for item in brands if "PREMIUM BUSINESS" in str(item.get("brand") or "").upper()]
    if not wanted:
        return None
    wanted.sort(key=lambda item: (item.get("miles") or 10**9, item.get("taxes") or 0))
    return wanted[0]


async def _choose_fare(page, index: int, fare: str) -> bool:
    await _close_latam_dialogs(page)
    header = page.locator(f'[data-testid="wrapper-card-header-{index}"]')
    try:
        if not await human_click(header, page=page, timeout=8000):
            return False
        await human_pause(page, 0.8, 1.8)
    except Exception:
        return False
    card = page.locator(f'[data-testid="wrapper-card-flight-{index}"]')
    if fare == "LIGHT":
        button = card.locator('[data-testid="bundle-detail-0-flight-select"]')
        try:
            if await human_click(button, page=page, timeout=8000):
                print(f"Escolhi a tarifa LIGHT (card {index}).", flush=True)
                await human_pause(page, 4.5, 7.5)
                return True
        except Exception:
            named = await _click_named(page, ("escolher a tarifa light", "^escolher$"))
            return bool(named)
        named = await _click_named(page, ("escolher a tarifa light", "^escolher$"))
        return bool(named)
    button = card.get_by_role("button", name=re.compile(r"premium business standard|tarifa premium business", re.I))
    try:
        if await button.count():
            if await human_click(button.first, page=page, timeout=8000):
                print(f"Escolhi a tarifa PREMIUM BUSINESS STANDARD (card {index}).", flush=True)
                try:
                    keep = page.get_by_role(
                        "button",
                        name=re.compile(r"continuar com a standard|continuar com standard", re.I),
                    )
                    await human_click(keep.first, page=page, timeout=10000)
                    print("Mantive Premium Business Standard no aviso de upgrade.", flush=True)
                except Exception:
                    modal = page.locator('[data-testid="fifth-brand-modal--dialog"]')
                    try:
                        await human_click(
                            modal.get_by_text(re.compile(r"continuar com a standard", re.I)),
                            page=page,
                            timeout=4000,
                        )
                        print("Mantive Premium Business Standard no aviso de upgrade.", flush=True)
                    except Exception:
                        pass
                await human_pause(page, 5.5, 9.0)
                return True
    except Exception:
        pass
    named = await _click_named(page, ("premium business standard", "escolher a tarifa premium business"))
    if named:
        print(f"Escolhi a executiva ({named}) no card {index}.", flush=True)
        return True
    print("Não achei tarifa executiva neste voo.", flush=True)
    return False


async def _choose_light_fare(page, index: int) -> bool:
    return await _choose_fare(page, index, "LIGHT")


async def _wait_volta(page, context, wait_loops: int):
    warned = False
    for _ in range(wait_loops):
        page = await _ensure_latam_page(context, page, quiet=warned)
        if "latamairlines.com" not in (page.url or "") or "booking.com" in (page.url or ""):
            warned = True
        ret = await _read_flight_cards(page)
        if ret.get("ok") and ret.get("leg") == "volta":
            return page, ret
        await page.wait_for_timeout(1000)
    return page, None


async def _go_back_to_outbound(page) -> bool:
    await _close_latam_dialogs(page)
    named = await _click_named(
        page,
        (
            "alterar voo de ida",
            "editar voo de ida",
            "escolher outro voo de ida",
            "voltar ao voo de ida",
            "escolher um voo de ida",
            "voo de ida",
        ),
    )
    if named:
        await page.wait_for_timeout(2500)
    else:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2500)
        except Exception:
            return False
    cards = await _read_flight_cards(page)
    return bool(cards.get("ok") and cards.get("leg") == "ida")


async def _force_ida_results(page, origin: str, dest: str, day: str, return_day: str | None) -> dict[str, Any]:
    cards = await _read_flight_cards(page)
    if cards.get("ok") and cards.get("leg") == "ida":
        return cards
    if cards.get("leg") == "volta":
        print("Estou na volta; volto para a lista de ida.", flush=True)
        if await _go_back_to_outbound(page):
            cards = await _read_flight_cards(page)
            if cards.get("ok") and cards.get("leg") == "ida":
                return cards
    print("Refaço a busca na home para pegar a ida.", flush=True)
    await _safe_goto(page, LATAM_HOME)
    await _dismiss_banners(page)
    await _pause(page, 1.8, 3.2)
    filled = await _search_from_home(page, origin, dest, day, return_day=return_day)
    if not filled:
        return {"ok": False, "cards": []}
    for _ in range(25):
        cards = await _read_flight_cards(page)
        if cards.get("ok") and cards.get("leg") == "ida":
            return cards
        await page.wait_for_timeout(1000)
    return cards


def _flight_key(card: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(card.get("origin") or "").replace("\n", " ").strip(),
        str(card.get("destination") or "").replace("\n", " ").strip(),
        str(card.get("duration") or "").strip(),
        card.get("stops"),
    )


def _match_card(cards: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, Any] | None:
    key = _flight_key(target)
    for card in cards:
        if _flight_key(card) == key:
            return card
    dep = _clock_from_block(target.get("origin") or "")
    dur = str(target.get("duration") or "").strip()
    for card in cards:
        if _clock_from_block(card.get("origin") or "") == dep and str(card.get("duration") or "").strip() == dur:
            return card
    return None




def _copy_ok_harvest(file_path: Path, origin: str, dest: str, day: str, job: dict[str, Any] | None = None) -> None:
    if not file_path.exists():
        return
    backup = harvest_ok_route_path("latam", origin, dest, day, job=job)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, backup)


def _card_face_miles(card: dict[str, Any]) -> int:
    return int(card["miles"])


def _merge_offer_dicts(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in old + new:
        key = (
            str(item.get("trecho") or item.get("trip_kind") or "round_trip"),
            str(item.get("tarifa") or item.get("fare") or ""),
            str(item.get("cabin") or "economy"),
            str(item.get("origin") or ""),
            str(item.get("destination") or ""),
            str(item.get("departure_time") or ""),
            str(item.get("return_time") or ""),
            item.get("milhas"),
        )
        merged[key] = item
    order = {"ida": 0, "volta": 1, "ida_volta": 2, "round_trip": 2}
    return sorted(
        merged.values(),
        key=lambda item: (
            order.get(str(item.get("trecho") or item.get("trip_kind") or ""), 9),
            str(item.get("tarifa") or ""),
            item.get("milhas") or 0,
        ),
    )


def _resolve_card_iata(raw: str, text: str | None, fallback: str) -> str:
    token = (raw or "").strip().upper()
    if token in CITY_MEMBER_CODES:
        return token
    parsed = iata_from_card(text, token or fallback)
    if parsed in CITY_MEMBER_CODES:
        return parsed
    if parsed and parsed not in CITY_AIRPORTS:
        return parsed
    fallback_token = (fallback or "").strip().upper()
    if fallback_token in CITY_MEMBER_CODES:
        return fallback_token
    return parsed or token or fallback_token


def _card_airports(card: dict[str, Any], fallback_origin: str, fallback_dest: str) -> tuple[str, str]:
    origin = _resolve_card_iata(str(card.get("originIata") or ""), card.get("origin"), fallback_origin)
    dest = _resolve_card_iata(str(card.get("destinationIata") or ""), card.get("destination"), fallback_dest)
    return origin, dest


def _offer_from_search(
    origin: str,
    dest: str,
    day: str,
    return_day: str | None,
    outbound: dict[str, Any],
    out_itin: dict[str, Any],
    inbound: dict[str, Any] | None,
    in_itin: dict[str, Any] | None,
    booking: str,
    cabin: str = "economy",
    fare: str = "LIGHT",
    trip_kind: str = "round_trip",
) -> Offer:
    chosen = inbound or outbound
    durations = [part for part in (outbound.get("duration"), (inbound or {}).get("duration")) if part]
    stops = outbound.get("stops")
    if inbound and inbound.get("stops") is not None:
        stops = (stops or 0) + inbound["stops"]
    return Offer(
        origin=origin,
        destination=dest,
        departure_date=day,
        return_date=return_day,
        airline="LATAM",
        stops=stops,
        cabin=cabin,
        price_type="miles",
        currency="BRL",
        price_cash=None,
        miles=int(chosen["miles"]),
        miles_program="latam",
        taxes=chosen.get("taxes"),
        source="latampass",
        booking_url=booking,
        departure_time=_clock_from_block(outbound.get("origin") or ""),
        arrival_time=_clock_from_block(outbound.get("destination") or ""),
        duration=" · ".join(durations) if durations else None,
        operators=None,
        layover=None,
        return_time=_clock_from_block((inbound or {}).get("origin") or ""),
        return_arrival=_clock_from_block((inbound or {}).get("destination") or ""),
        fare=fare,
        segments=[
            {"leg": "ida", "stops": outbound.get("stops"), "duration": outbound.get("duration")},
            *([{"leg": "volta", "stops": inbound.get("stops"), "duration": inbound.get("duration")}] if inbound else []),
        ],
        trip_kind=trip_kind,
    )


def _offer_leg(
    origin: str,
    dest: str,
    day: str,
    card: dict[str, Any],
    itin: dict[str, Any] | None,
    booking: str,
    cabin: str,
    fare: str,
    trip_kind: str,
) -> Offer:
    return Offer(
        origin=origin,
        destination=dest,
        departure_date=day,
        return_date=None,
        airline="LATAM",
        stops=card.get("stops"),
        cabin=cabin,
        price_type="miles",
        currency="BRL",
        price_cash=None,
        miles=int(card["miles"]),
        miles_program="latam",
        taxes=card.get("taxes"),
        source="latampass",
        booking_url=booking,
        departure_time=_clock_from_block(card.get("origin") or ""),
        arrival_time=_clock_from_block(card.get("destination") or ""),
        duration=card.get("duration") or None,
        operators=None,
        layover=None,
        fare=fare,
        segments=[{"leg": trip_kind, "stops": card.get("stops"), "duration": card.get("duration")}],
        trip_kind=trip_kind,
    )


def _write_one_harvest_file(
    raw_dir,
    job: dict[str, Any],
    status: str,
    url: str,
    compact: list[dict[str, Any]],
    slim: dict[str, Any],
):
    origin = job["origin"]
    dest = job["destination"]
    day = job["day"]
    file_path = harvest_route_path(raw_dir, origin, dest, day, job=job)
    ok = status in {"ok", 200} and bool(compact)
    if not ok:
        if file_path.exists():
            print(
                f"Mantenho {file_path.parent.parent.name}/{file_path.parent.name}/{file_path.name}; a busca nova não veio completa.",
                flush=True,
            )
        return file_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(
            {"job": job, "status": status, "url": url, "offers": compact, "payload": slim},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _copy_ok_harvest(file_path, origin, dest, day, job=job)
    try:
        shown = file_path.relative_to(raw_dir)
    except ValueError:
        shown = Path(file_path.parent.parent.name) / file_path.parent.name / file_path.name
    print(f"Arquivo {shown}: {len(compact)} ofertas.", flush=True)
    return file_path


def _write_harvest_file(
    raw_dir,
    job: dict[str, Any],
    status: str,
    url: str,
    compact: list[dict[str, Any]],
    slim: dict[str, Any],
):
    if not compact:
        return harvest_route_path(raw_dir, job["origin"], job["destination"], job["day"], job=job)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in compact:
        origin = str(item.get("origin") or job["origin"]).strip().upper()
        dest = str(item.get("destination") or job["destination"]).strip().upper()
        day = str(item.get("departure_date") or job["day"])
        groups.setdefault((origin, dest, day), []).append(item)
    last = None
    for (origin, dest, day), items in groups.items():
        last = _write_one_harvest_file(
            raw_dir,
            {
                **job,
                "origin": origin,
                "destination": dest,
                "day": day,
                "city": job.get("city"),
                "search_origin": job.get("search_origin") or job["origin"],
            },
            status,
            url,
            items,
            slim,
        )
    return last


def _compact_offer(offer: Offer) -> dict[str, Any]:
    trecho = {"ida": "ida", "volta": "volta", "round_trip": "ida_volta"}.get(
        offer.trip_kind or "round_trip", offer.trip_kind
    )
    return {
        "origin": offer.origin,
        "destination": offer.destination,
        "departure_date": offer.departure_date,
        "return_date": offer.return_date,
        "departure_time": offer.departure_time,
        "arrival_time": offer.arrival_time,
        "return_time": offer.return_time,
        "return_arrival": offer.return_arrival,
        "airline": offer.airline,
        "operators": offer.operators,
        "stops": offer.stops,
        "duration": offer.duration,
        "conexoes": offer.layover,
        "tarifa": offer.fare,
        "cabin": offer.cabin,
        "trecho": trecho,
        "trip_kind": offer.trip_kind,
        "milhas": offer.miles,
        "taxas": offer.taxes,
        "trechos": offer.segments,
        "program": offer.miles_program,
        "source": offer.source,
        "booking_url": offer.booking_url,
    }


def _dedupe_offers(offers: list[Offer]) -> list[Offer]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[Offer] = []
    for offer in offers:
        key = (
            offer.trip_kind,
            offer.origin,
            offer.destination,
            offer.departure_date,
            offer.return_date,
            offer.cabin,
            offer.fare,
            offer.departure_time,
            offer.return_time,
            offer.miles,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)
    return unique


async def _ida_list(page, origin: str, dest: str, day: str, return_day: str | None) -> dict[str, Any]:
    cards = await _read_flight_cards(page)
    if cards.get("ok") and cards.get("leg") == "ida":
        return cards
    if await _go_back_to_outbound(page):
        cards = await _read_flight_cards(page)
        if cards.get("ok") and cards.get("leg") == "ida":
            return cards
    return await _force_ida_results(page, origin, dest, day, return_day)


async def _choose_first_fare(page, cards: list[dict[str, Any]], fare: str) -> dict[str, Any] | None:
    if not cards:
        return None
    card = cards[0]
    if await _choose_fare(page, card["index"], fare):
        return card
    return None


async def _collect_job_offers(page, context, job: dict[str, Any], cards: dict[str, Any], url: str, wait_loops: int):
    origin, dest, day = job["origin"], job["destination"], job["day"]
    return_day = job.get("return_day") or job.get("return_date")
    international = str(job.get("kind") or "").lower() == "international"
    raw_outbound = list(cards.get("cards") or [])
    outbound_cards = [
        card
        for card in raw_outbound
        if plausible_miles(card.get("miles"), origin, dest, trip_kind="ida")
    ]
    outbound_cards.sort(key=lambda card: int(card.get("miles") or 10**9))
    skipped_ida = len(raw_outbound) - len(outbound_cards)
    offers: list[Offer] = []
    if skipped_ida:
        print(f"Ignoro {skipped_ida} idas LATAM acima do teto do trecho.", flush=True)
    if not outbound_cards:
        return page, offers, "empty"

    print(
        f"{len(outbound_cards)} voos de ida {origin}-{dest} {day}. "
        f"Gravo o aeroporto real de cada card. Sem abrir itinerário.",
        flush=True,
    )
    ida_booking = latam_url(origin, dest, day, True, return_date=None)
    volta_booking = latam_url(dest, origin, return_day, True, return_date=None) if return_day else url
    gig = sdu = cgh = gru = vcp = 0
    for card in outbound_cards:
        out_orig, out_dest = _card_airports(card, origin, dest)
        if out_orig == "GIG":
            gig += 1
        elif out_orig == "SDU":
            sdu += 1
        elif out_orig == "CGH":
            cgh += 1
        elif out_orig == "GRU":
            gru += 1
        elif out_orig == "VCP":
            vcp += 1
        offers.append(_offer_leg(out_orig, out_dest, day, card, {}, ida_booking, "economy", "LIGHT", "ida"))
    print(
        f"Gravo idas LIGHT {origin}-{dest} {day} por cidade "
        f"({gig} GIG, {sdu} SDU, {cgh} CGH, {gru} GRU, {vcp} VCP, paradas + duração).",
        flush=True,
    )
    if not return_day:
        return page, _dedupe_offers(offers), "ok"

    await page.wait_for_timeout(4000)
    print("Escolho a LIGHT mais barata da ida. Na volta leio LIGHT e executiva, sem nova busca.", flush=True)
    picked = await _choose_first_fare(page, outbound_cards, "LIGHT")
    if not picked:
        print("Não escolhi LIGHT; fico só com as idas.", flush=True)
        return page, _dedupe_offers(offers), "ok"
    picked_origin, picked_dest = _card_airports(picked, origin, dest)
    await page.wait_for_timeout(4000)
    page, ret_cards = await _wait_volta(page, context, wait_loops)
    if not ret_cards:
        print("A volta não carregou; fico só com as idas. Não refaço a busca.", flush=True)
        return page, _dedupe_offers(offers), "ok"

    returns = list(ret_cards.get("cards") or [])
    print(
        "Voos de volta: gravo só as milhas que aparecem no card, sem inventar trecho por subtração.",
        flush=True,
    )
    print(f"{len(returns)} voos de volta. Gravo LIGHT e, se houver, a executiva de cada um.", flush=True)
    for ret in returns:
        in_orig, in_dest = _card_airports(ret, dest, picked_origin)
        volta_miles = _card_face_miles(ret)
        if plausible_miles(volta_miles, in_orig, in_dest, trip_kind="volta"):
            offers.append(
                _offer_leg(
                    in_orig,
                    in_dest,
                    return_day,
                    ret,
                    {},
                    volta_booking,
                    "economy",
                    "LIGHT",
                    "volta",
                )
            )
        elif volta_miles:
            print(
                f"Ignoro volta {volta_miles} milhas {in_orig}-{in_dest}; fora do teto do trecho.",
                flush=True,
            )
        if not international:
            continue
        await page.wait_for_timeout(2500)
        try:
            brands = await _read_card_brands(page, ret["index"])
        except Exception:
            brands = []
        biz = _cheapest_business_brand(brands)
        if not biz:
            continue
        priced = {**ret, "miles": biz["miles"], "taxes": biz.get("taxes")}
        if int(priced["miles"] or 0) <= int(ret.get("miles") or 0):
            continue
        volta_miles = _card_face_miles(priced)
        if plausible_miles(volta_miles, in_orig, in_dest, "business", trip_kind="volta"):
            offers.append(
                _offer_leg(
                    in_orig,
                    in_dest,
                    return_day,
                    priced,
                    {},
                    volta_booking,
                    "business",
                    biz["brand"],
                    "volta",
                )
            )

    offers = [
        item
        for item in _dedupe_offers(offers)
        if plausible_miles(
            item.miles,
            item.origin,
            item.destination,
            item.cabin,
            fare=item.fare,
            trip_kind=item.trip_kind,
        )
    ]
    idas = sum(1 for item in offers if item.trip_kind == "ida")
    voltas = sum(1 for item in offers if item.trip_kind == "volta")
    rt = sum(1 for item in offers if item.trip_kind == "round_trip")
    print(
        f"Pronto {origin}-{dest}: {idas} idas em {day}, {voltas} voltas em {return_day}, {rt} totais. Sem nova busca.",
        flush=True,
    )
    return page, offers, "ok"


def _browser_closed(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "has been closed" in message or "targetclosed" in message or "target closed" in message


async def _safe_goto(page, url: str) -> None:
    last: BaseException | None = None
    delays = (20, 40, 60, 90)
    for attempt, delay in enumerate((*delays, None)):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            if await _page_shows_http2(page):
                raise LatamHttp2Error(page.url or url)
            return
        except LatamHttp2Error:
            raise
        except Exception as exc:
            last = exc
            if _browser_closed(exc):
                raise
            message = str(exc)
            if _looks_http2(message):
                raise LatamHttp2Error(message.splitlines()[0])
            if "ERR_ABORTED" in message or "interrupted" in message.lower():
                await page.wait_for_timeout(4000)
                return
            flaky = any(
                token in message
                for token in (
                    "ERR_CONNECTION",
                    "ERR_NETWORK",
                    "ERR_TIMED_OUT",
                    "Timeout",
                    "net::",
                )
            )
            if not flaky or delay is None:
                href = (page.url or "")
                if "latamairlines.com" in href:
                    print(f"Sigo na aba LATAM atual depois do erro de rede: {href[:90]}", flush=True)
                    return
                raise
            print(
                f"A home não carregou ({message.splitlines()[0]}). Tento de novo em {delay}s.",
                flush=True,
            )
            await page.wait_for_timeout(delay * 1000)
    if last:
        raise last


async def _pause(page, low: float = 0.4, high: float = 1.1) -> None:
    await human_pause(page, low, high)


async def _rest_on_results_then_home(page, home: str, seconds: float) -> bool:
    try:
        return await rest_like_a_person(page, home, seconds, _safe_goto)
    except LatamHttp2Error:
        raise
    except Exception as exc:
        if _browser_closed(exc):
            print("A janela do Chrome fechou no intervalo. O que já salvou permanece.", flush=True)
            return False
        print(f"Não voltei à home agora ({exc}). Sigo para a próxima busca mesmo assim.", flush=True)
        return True


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
    host = host_page(root)
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in roles:
            target = await _first_visible(root.get_by_role(role, name=pattern), timeout=700)
            if target is None:
                continue
            if await human_click(target, page=host, timeout=5000):
                return label
    return None


async def _dismiss_banners(page) -> None:
    await _click_named(
        page,
        (
            "aceite todos os cookies",
            "aceitar todos os cookies",
            "aceite todos",
            "aceitar todos",
            "aceitar cookies",
            "^aceitar$",
            "^aceito$",
            "concordo",
        ),
    )
    try:
        cookie = page.locator("#onetrust-accept-btn-handler")
        if await cookie.count() and await cookie.first.is_visible(timeout=800):
            await human_click(cookie.first, page=page, timeout=4000)
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
    await human_type(page, field, text)


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
        await human_click(option, page=page)
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
        await human_click(field, page=page)
        await _pause(page, 0.35, 0.8)
        try:
            focused = await _first_visible(root.locator("input:focus"), timeout=500)
            typer = focused or field
            await human_type(page, typer, shown)
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
        await human_click(next_btn, page=page)
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
    await human_click(day_btn, page=page)
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
        await human_click(box, page=host_page(root), timeout=4000)
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
    token = code.upper()
    if token in CITY_AIRPORTS:
        option = await _first_visible(
            page.get_by_role("option").filter(
                has_text=re.compile(r"todos os aeroportos|\bRIO\b|\bSAO\b|s[aã]o paulo|rio de janeiro", re.I)
            ),
            timeout=2500,
        )
    else:
        option = await _first_visible(
            page.get_by_role("option").filter(has_text=re.compile(rf"\b{code}\b", re.I)),
            timeout=2500,
        )
    if option is not None:
        await human_click(option, page=page)
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
                await human_click(day_btn, page=page)
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
        await human_click(next_btn, page=page)
        await _pause(page, 0.3, 0.55)
    return False


async def _pick_priced_date(page, day: str, field_sel: str = "#fsb-departure--text-field", close: bool = True) -> bool:
    field = page.locator(field_sel)
    if await field.count():
        await human_click(field, page=page)
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
            if await human_click(target, page=page, timeout=4000):
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
        if await _latam_login_gate_visible(page):
            print(
                "A LATAM pediu login. Não preencho senha. Entre você no Chrome; eu espero.",
                flush=True,
            )
            if not await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK):
                return False
            await _safe_goto(page, LATAM_HOME)
            await _dismiss_banners(page)
        voos = page.locator("#id-tab-flight")
        if await voos.count():
            if not await human_click(voos, page=page, timeout=4000):
                await _close_latam_dialogs(page)
                await human_click(voos, page=page, timeout=4000)
            await _pause(page, 0.35, 0.7)
        origin_field = page.locator("#fsb-origin--text-field")
        try:
            form_visible = await origin_field.is_visible(timeout=1500)
        except Exception:
            form_visible = False
        if not form_visible:
            toggle = page.locator("#fsb-form-toggle")
            if await toggle.count():
                await human_click(toggle, page=page)
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
                clicked = await human_click(search, page=page, timeout=8000)
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
                if await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK):
                    await context.close()
                    return {"ok": True, "url": page.url, "state": "logged"}
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
        print(
            "Abro a LATAM com os cookies salvos. Se pedir login, entre você; não preencho senha.",
            flush=True,
        )
        deadline = asyncio.get_event_loop().time() + 12 * 60
        href = page.url
        ok = False
        state = await latam_page_state(page)
        if await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK):
            href = page.url
            await context.close()
            return {"ok": True, "url": href, "state": "logged"}
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
        context, page, attached = await _connect_or_launch(playwright)
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

        try:
            context.on("response", on_response)
        except Exception:
            page.on("response", on_response)
        if "latamairlines.com" not in (page.url or ""):
            await _safe_goto(page, LATAM_HOME)
        await _dismiss_banners(page)
        if not await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK):
            print("Não começo as buscas sem sessão. Deixo o Chrome aberto na home.", flush=True)
            try:
                await _park_latam_home(page)
            except Exception:
                pass
            return []
        print("Pausa entre buscas irregular, sem cadência fixa. Leio os cards da LATAM.", flush=True)
        await wander_mouse(page)

        index = 0
        recycles = 0
        while index < len(jobs):
            job = jobs[index]
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
                city_note = city_search_note(origin, dest)
                if _url_matches_job(page.url or "", origin, dest, day, return_day):
                    print(f"Já estou nos resultados {origin} para {dest} {day}{volta}. Leio a tela.", flush=True)
                    filled = True
                else:
                    print(f"Home LATAM: {origin} para {dest} {day}{volta}{city_note}", flush=True)
                    await _close_latam_dialogs(page)
                    await _safe_goto(page, LATAM_HOME)
                    await _dismiss_banners(page)
                    await _pause(page, 1.8, 3.2)
                    filled = await _search_from_home(page, origin, dest, day, return_day=return_day)
                    if not filled:
                        print("O formulário da home não fechou; abro o link da busca uma vez.", flush=True)
                        await _close_latam_dialogs(page)
                        await _safe_goto(page, url)
                        filled = True
                if not filled:
                    status = "error"
                    payload = {"error": "form_fill_failed", "url": page.url}
                else:
                    page = await _ensure_latam_page(context, page)
                    text = ""
                    cards: dict[str, Any] = {}
                    for _ in range(wait_loops):
                        page = await _ensure_latam_page(context, page)
                        cards = await _read_flight_cards(page)
                        if cards.get("ok"):
                            await browse_results(page)
                            break
                        try:
                            text = await page.inner_text("body")
                        except Exception:
                            text = ""
                        low = text.lower()
                        if "não encontramos" in low or "nao encontramos" in low:
                            break
                        if "application error" in low or "client-side exception" in low:
                            break
                        if "demorando mais" in low or "tente novamente" in low:
                            break
                        if _looks_http2(text):
                            raise LatamHttp2Error(page.url or "ERR_HTTP2_PROTOCOL_ERROR")
                        await page.wait_for_timeout(1000)
                    blocked = "403" in " ".join(captured.get("urls") or []) or "demorando mais" in (text or "").lower()
                    state = await latam_page_state(page)
                    href = page.url or ""
                    payload["url"] = href
                    payload["network"] = captured.get("urls") or []
                    if captured.get("bff_url"):
                        payload["bff_url"] = captured.get("bff_url")
                        payload["bff_status"] = captured.get("bff_status")
                    if "booking.com" in href or "latamairlines.com" not in href:
                        status = "error"
                        payload["error"] = "left_latam"
                    elif state == "login" and not cards.get("ok"):
                        if not await login_with_env(page, "latam", LATAM_HOME, session_mark=SESSION_MARK):
                            status = "login"
                            SESSION_MARK.unlink(missing_ok=True)
                            payload["error"] = "login_required"
                        else:
                            print("Sessão LATAM pronta. Refaço o preenchimento desta busca.", flush=True)
                            await _safe_goto(page, LATAM_HOME)
                            await _dismiss_banners(page)
                            filled = await _search_from_home(page, origin, dest, day, return_day=return_day)
                            if filled:
                                for _ in range(wait_loops):
                                    cards = await _read_flight_cards(page)
                                    if cards.get("ok"):
                                        await browse_results(page)
                                        break
                                    await page.wait_for_timeout(1000)
                            if not cards.get("ok"):
                                status = "login"
                                payload["error"] = "login_required"
                            else:
                                payload["leg"] = cards.get("leg")
                                payload["outbound"] = cards.get("cheapest")
                                page, offers, collect_status = await _collect_job_offers(
                                    page, context, job, cards, url, wait_loops
                                )
                                if collect_status != "ok":
                                    status = collect_status
                                if not offers and status == "ok":
                                    status = "empty"
                    elif blocked and not cards.get("ok"):
                        status = "throttled"
                        payload["error"] = "throttled"
                    elif state == "denied":
                        status = "denied"
                        payload["error"] = "denied"
                    elif not cards.get("ok"):
                        status = "empty" if "não encontramos" in (text or "").lower() else "error"
                        payload["error"] = cards.get("reason") or "no_flight_cards"
                    else:
                        cards = await _read_flight_cards(page)
                        if cards.get("leg") == "volta":
                            print("Estou na volta desta busca; volto à lista de ida sem pesquisar de novo.", flush=True)
                            await _go_back_to_outbound(page)
                            cards = await _read_flight_cards(page)
                        if not cards.get("ok") or cards.get("leg") != "ida":
                            status = "error"
                            payload["error"] = "not_on_outbound"
                        else:
                            payload["leg"] = cards.get("leg")
                            payload["outbound"] = cards.get("cheapest")
                            page, offers, collect_status = await _collect_job_offers(
                                page, context, job, cards, url, wait_loops
                            )
                            if collect_status != "ok":
                                status = collect_status
                            if not offers and status == "ok":
                                status = "empty"
            except LatamHttp2Error as exc:
                recycles += 1
                print(f"HTTP/2 na LATAM ({str(exc).splitlines()[0][:160]}).", flush=True)
                if recycles > 12:
                    print("HTTP/2 demais nesta leva. Paro; o que já salvou permanece.", flush=True)
                    break
                context, page, attached = await _recycle_latam_browser(playwright, context, on_response)
                await asyncio.sleep(8)
                continue
            except Exception as exc:
                status = "error"
                payload = {"error": str(exc)}
                print(f"Falha nesta busca {origin}-{dest}: {exc}", flush=True)
            compact = [_compact_offer(offer) for offer in offers]
            if save_raw:
                slim = dict(payload)
                slim.pop("bff", None)
                slim.pop("dom_text", None)
                ida_compact = [
                    item for item in compact if item.get("trip_kind") in {None, "ida", "round_trip"}
                ]
                volta_compact = [item for item in compact if item.get("trip_kind") == "volta"]
                file_path = _write_harvest_file(raw_dir, job, status, url, ida_compact or compact, slim)
                if volta_compact and return_day:
                    volta_job = {
                        **job,
                        "origin": dest,
                        "destination": origin,
                        "day": return_day,
                        "return_day": None,
                        "leg": "volta",
                    }
                    volta_url = latam_url(dest, origin, return_day, True, return_date=None)
                    _write_harvest_file(raw_dir, volta_job, status, volta_url, volta_compact, slim)
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
            if index < len(jobs) - 1 and status in {"ok", "empty", "ida_only"}:
                try:
                    if not await _rest_on_results_then_home(page, LATAM_HOME, wait):
                        break
                    nxt = jobs[index + 1]
                    if (nxt.get("region") or "") != (job.get("region") or ""):
                        gap = irregular_region_seconds()
                        print(
                            f"Fico na home {gap / 60:.1f} min antes de {nxt.get('region_label')}. "
                            "Não fecho o Chrome nem a sessão.",
                            flush=True,
                        )
                        await _park_latam_home(page)
                        await idle_on_page(page, gap)
                except LatamHttp2Error as exc:
                    recycles += 1
                    print(f"HTTP/2 na LATAM ao voltar para a home ({str(exc).splitlines()[0][:160]}).", flush=True)
                    if recycles > 12:
                        print("HTTP/2 demais nesta leva. Paro; o que já salvou permanece.", flush=True)
                        break
                    context, page, attached = await _recycle_latam_browser(playwright, context, on_response)
            index += 1
        try:
            await _park_latam_home(page)
        except Exception:
            pass
        print("Terminei esta leva. O Chrome fica aberto na home, com a sessão.", flush=True)
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
