from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.airports import expand_city_airports
from app.config import CIA_HEADLESS, CIA_PAUSE_SECONDS, DATA_DIR, harvest_route_path
from app.providers.base import Offer

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DATA_DIR / "playwright-browsers"))

MILES_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})+|\d{4,7})\s*(?:milhas|pts|points|pontos)",
    re.I,
)


def chromium_args() -> list[str]:
    return [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ]


def browser_closed(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "has been closed" in message or "targetclosed" in message or "target closed" in message


async def launch_context(playwright, profile_dir: Path, headless: bool | None = None):
    profile_dir.mkdir(parents=True, exist_ok=True)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",
        headless=CIA_HEADLESS if headless is None else headless,
        args=chromium_args(),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 900},
    )


async def safe_goto(page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        if browser_closed(exc):
            raise
        message = str(exc)
        if "ERR_ABORTED" in message or "interrupted" in message.lower():
            await page.wait_for_timeout(4000)
            return
        raise


async def pause(page, low: float = 0.4, high: float = 1.1) -> None:
    await page.wait_for_timeout(int(random.uniform(low, high) * 1000))


async def first_visible(locator, timeout: int = 2000):
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


async def click_named(root, labels: tuple[str, ...], roles: tuple[str, ...] = ("button", "radio", "tab", "checkbox")):
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in roles:
            target = await first_visible(root.get_by_role(role, name=pattern), timeout=700)
            if target is not None:
                await target.click()
                return label
    return None


async def dismiss_banners(page) -> None:
    await click_named(
        page,
        (
            "aceitar todos os cookies",
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
            await cookie.first.click()
    except Exception:
        pass


async def type_slowly(page, field, text: str) -> None:
    await field.click()
    await pause(page, 0.25, 0.55)
    try:
        await field.fill("")
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    await pause(page, 0.2, 0.45)
    try:
        await field.press_sequentially(text, delay=random.randint(90, 170))
    except Exception:
        await page.keyboard.type(text, delay=random.randint(90, 170))


async def find_field(root, labels: tuple[str, ...], extra=None):
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in ("combobox", "textbox", "searchbox", "button"):
            field = await first_visible(root.get_by_role(role, name=pattern), timeout=800)
            if field is not None:
                return field
        field = await first_visible(root.get_by_placeholder(pattern), timeout=600)
        if field is not None:
            return field
        field = await first_visible(root.get_by_label(pattern), timeout=600)
        if field is not None:
            return field
    if extra is not None:
        field = await first_visible(extra, timeout=800)
        if field is not None:
            return field
    return None


async def fill_airport(page, root, labels: tuple[str, ...], code: str, extra=None) -> bool:
    field = await find_field(root, labels, extra=extra)
    if field is None:
        cities = root.get_by_placeholder(re.compile(r"cidade|aeroporto|origem|destino", re.I))
        field = await first_visible(cities, timeout=800)
    if field is None:
        return False
    await type_slowly(page, field, code)
    await pause(page, 0.7, 1.4)
    pattern = re.compile(r"todos os aeroportos|\bRIO\b", re.I) if code.upper() == "RIO" else re.compile(rf"\b{code}\b", re.I)
    option = await first_visible(page.get_by_role("option").filter(has_text=pattern), timeout=2500)
    if option is not None:
        await option.click()
        return True
    await page.keyboard.press("ArrowDown")
    await pause(page, 0.2, 0.4)
    await page.keyboard.press("Enter")
    return True


async def click_calendar_day(page, day: str) -> bool:
    day_btn = page.locator(f"#date-{day}, [data-testid='date-{day}'], [data-date='{day}']")
    parsed = date.fromisoformat(day)
    for _ in range(16):
        try:
            if await day_btn.first.is_visible(timeout=600):
                await day_btn.first.click()
                await pause(page, 0.35, 0.7)
                return True
        except Exception:
            pass
        cell = await first_visible(
            page.get_by_role("gridcell", name=re.compile(rf"\b{parsed.day}\b")).or_(
                page.get_by_role("button", name=re.compile(rf"^{parsed.day}$"))
            ),
            timeout=500,
        )
        if cell is not None:
            await cell.click()
            await pause(page, 0.35, 0.7)
            return True
        next_btn = await first_visible(
            page.get_by_role("button", name=re.compile(r"próximo mês|mes seguinte|next month|ir para o próximo", re.I)),
            timeout=400,
        )
        if next_btn is None:
            break
        await next_btn.click()
        await pause(page, 0.3, 0.55)
    return False


async def pick_dates(page, outbound: str, inbound: str | None) -> bool:
    shown = date.fromisoformat(outbound).strftime("%d/%m/%Y")
    field = await find_field(
        page,
        ("data de ida", "data da ida", "partida", "^ida$", "departure"),
        extra=page.get_by_placeholder(re.compile(r"dd/mm|aaaa|ida", re.I)),
    )
    if field is not None:
        await field.click()
        await pause(page, 0.4, 0.8)
        try:
            await type_slowly(page, field, shown)
            await page.keyboard.press("Enter")
            await pause(page, 0.3, 0.6)
        except Exception:
            if not await click_calendar_day(page, outbound):
                return False
    elif not await click_calendar_day(page, outbound):
        return False
    if not inbound:
        return True
    back = date.fromisoformat(inbound).strftime("%d/%m/%Y")
    ret = await find_field(
        page,
        ("data de volta", "data da volta", "regresso", "^volta$", "return"),
        extra=page.get_by_placeholder(re.compile(r"volta|return", re.I)),
    )
    if ret is not None:
        try:
            await type_slowly(page, ret, back)
            await page.keyboard.press("Enter")
            await pause(page, 0.3, 0.6)
            return True
        except Exception:
            pass
    return await click_calendar_day(page, inbound)


async def mark_checkbox(page, labels: tuple[str, ...]) -> bool:
    clicked = await click_named(page, labels, roles=("checkbox", "switch", "radio", "button"))
    if clicked:
        return True
    for label in labels:
        text = await first_visible(page.get_by_text(re.compile(label, re.I)), timeout=800)
        if text is not None:
            await text.click(force=True)
            return True
    return False


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


def json_mentions_place(body: Any, code: str) -> bool:
    blob = json.dumps(body, ensure_ascii=False).lower()
    return any(item.lower() in blob for item in [code, *expand_city_airports(code)])


def looks_like_offers(body: Any, keys: tuple[str, ...]) -> bool:
    if not isinstance(body, dict):
        return False
    return any(key in body for key in keys)


async def collect_home_jobs(
    jobs: list[dict[str, Any]],
    raw_dir: Path,
    *,
    profile_dir: Path,
    home: str,
    label: str,
    program: str,
    bff_hints: tuple[str, ...],
    json_keys: tuple[str, ...],
    round_labels: tuple[str, ...],
    oneway_labels: tuple[str, ...],
    origin_labels: tuple[str, ...],
    dest_labels: tuple[str, ...],
    miles_labels: tuple[str, ...],
    search_labels: tuple[str, ...],
    blocked_hints: tuple[str, ...],
    parse_offers: Callable[..., list[Offer]],
    booking_url: Callable[..., str],
    pause_seconds: float | None = None,
    halt_on: set[str] | None = None,
    save_raw: bool = True,
    wait_loops: int = 30,
    on_result=None,
    session_mark: Path | None = None,
) -> list[dict[str, Any]]:
    from playwright.async_api import async_playwright

    wait = CIA_PAUSE_SECONDS if pause_seconds is None else pause_seconds
    stop = halt_on if halt_on is not None else {"login", "denied"}
    raw_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        context = await launch_context(playwright, profile_dir)
        page = context.pages[0] if context.pages else await context.new_page()
        captured: dict[str, Any] = {}
        wanted: dict[str, str] = {"origin": "", "dest": "", "day": ""}

        async def on_response(response) -> None:
            url = response.url.lower()
            hinted = any(hint in url for hint in bff_hints)
            if response.status != 200 and not hinted:
                if response.status in {403, 429} and hinted:
                    captured["bff_error"] = f"status={response.status}"
                    captured.setdefault("urls", []).append(f"{response.status} {response.url[:240]}")
                return
            try:
                body = await response.json()
            except Exception:
                if hinted:
                    captured.setdefault("urls", []).append(f"{response.status} {response.url[:240]}")
                    captured["bff_error"] = f"status={response.status} json_failed"
                return
            if not isinstance(body, dict):
                return
            if not hinted and not looks_like_offers(body, json_keys):
                return
            origin, dest, day = wanted["origin"], wanted["dest"], wanted["day"]
            if origin and not json_mentions_place(body, origin) and origin.lower() not in url:
                if not any(code.lower() in url for code in expand_city_airports(origin)):
                    return
            captured.setdefault("urls", []).append(f"{response.status} {response.url[:240]}")
            if response.status != 200:
                captured["bff_error"] = f"status={response.status}"
                return
            captured["bff"] = body
            captured["bff_url"] = response.url
            captured["bff_status"] = response.status

        page.on("response", on_response)

        for index, job in enumerate(jobs):
            captured.clear()
            origin, dest, day = job["origin"], job["destination"], job["day"]
            return_day = job.get("return_day") or job.get("return_date")
            wanted.update({"origin": origin, "dest": dest, "day": day})
            status = "ok"
            payload: dict[str, Any] = {}
            offers: list[Offer] = []
            try:
                volta = f" / volta {return_day}" if return_day else ""
                rio_note = " (RIO cobre GIG e SDU)" if origin == "RIO" or dest == "RIO" else ""
                print(f"Home {label}: {origin} → {dest} {day}{volta}{rio_note}", flush=True)
                await safe_goto(page, home)
                await dismiss_banners(page)
                await pause(page, 1.6, 2.8)
                body_text = ""
                try:
                    body_text = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                if any(hint in body_text for hint in blocked_hints):
                    status = "throttled"
                    payload = {"error": "throttled", "url": page.url}
                else:
                    if return_day:
                        trip = await click_named(page, round_labels)
                        print(f"Trecho: {trip or 'ida e volta'}.", flush=True)
                    else:
                        trip = await click_named(page, oneway_labels)
                        print(f"Trecho: {trip or 'somente ida'}.", flush=True)
                    await pause(page, 0.4, 0.9)
                    if not await fill_airport(page, page, origin_labels, origin):
                        raise RuntimeError("form_fill_failed:origem")
                    print(f"Origem {origin} preenchida.", flush=True)
                    await pause(page, 0.5, 1.0)
                    if not await fill_airport(page, page, dest_labels, dest):
                        raise RuntimeError("form_fill_failed:destino")
                    print(f"Destino {dest} preenchido.", flush=True)
                    await pause(page, 0.4, 0.9)
                    if not await pick_dates(page, day, return_day):
                        raise RuntimeError("form_fill_failed:data")
                    print("Datas preenchidas.", flush=True)
                    if miles_labels:
                        marked = await mark_checkbox(page, miles_labels)
                        print("Pontos/milhas marcados." if marked else "Não achei o interruptor de milhas; sigo.", flush=True)
                    await pause(page, 0.4, 0.9)
                    search = await click_named(page, search_labels)
                    if not search:
                        raise RuntimeError("form_fill_failed:buscar")
                    print("Cliquei em procurar voos.", flush=True)
                    text = ""
                    for _ in range(wait_loops):
                        if isinstance(captured.get("bff"), dict):
                            break
                        try:
                            text = await page.inner_text("body")
                        except Exception:
                            text = ""
                        low = text.lower()
                        if any(hint in low for hint in blocked_hints) or "demorando mais" in low:
                            break
                        if miles_from_text(text):
                            break
                        await page.wait_for_timeout(1000)
                    payload["url"] = page.url
                    payload["network"] = captured.get("urls") or []
                    if captured.get("bff_url"):
                        payload["bff_url"] = captured.get("bff_url")
                        payload["bff_status"] = captured.get("bff_status")
                    blocked = "403" in " ".join(captured.get("urls") or []) or any(
                        hint in (text or "").lower() for hint in blocked_hints
                    )
                    if isinstance(captured.get("bff"), dict):
                        offers = parse_offers(captured["bff"], origin, dest, day, return_day)
                        if not offers:
                            status = "empty"
                    elif blocked:
                        status = "throttled"
                        payload["error"] = "throttled"
                    else:
                        miles = miles_from_text(text)
                        payload["dom_text"] = (text or "")[:4000]
                        if miles:
                            offers = parse_offers(
                                {"miles": miles, "source": "dom"},
                                origin,
                                dest,
                                day,
                                return_day,
                            )
                        if not offers:
                            status = "empty"
            except Exception as exc:
                message = str(exc)
                if message.startswith("form_fill_failed"):
                    status = "error"
                    payload = {"error": message, "url": page.url}
                    print(f"Não consegui preencher a busca na home ({message}).", flush=True)
                elif browser_closed(exc):
                    status = "error"
                    payload = {"error": "browser_closed"}
                else:
                    status = "error"
                    payload = {"error": message}
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
            url = booking_url(origin, dest, day, return_day)
            if save_raw:
                slim = {k: v for k, v in payload.items() if k not in {"bff", "dom_text"}}
                file_path = harvest_route_path(raw_dir, origin, dest, day)
                keep_old = False
                if not compact and file_path.exists():
                    try:
                        previous = json.loads(file_path.read_text(encoding="utf-8"))
                        keep_old = bool(previous.get("offers"))
                    except (OSError, json.JSONDecodeError):
                        keep_old = False
                if not keep_old:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
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
                print(f"{label} limitou as buscas. Encerrando esta leva.", flush=True)
                break
            if status == "error" and str(payload.get("error") or "").startswith("form_fill_failed"):
                print("Não consegui preencher a busca na home. Parando para não insistir.", flush=True)
                break
            if status in stop:
                if status == "login" and session_mark:
                    session_mark.unlink(missing_ok=True)
                break
            if index < len(jobs) - 1 and status in {"ok", "empty"}:
                try:
                    await safe_goto(page, home)
                except Exception:
                    pass
                jitter = wait * (0.75 + random.random() * 0.5)
                await page.wait_for_timeout(int(jitter * 1000))
        await context.close()
    return results


async def open_login(profile_dir: Path, home: str, label: str, logged_hints: tuple[str, ...], session_mark: Path) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        context = await launch_context(playwright, profile_dir, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await safe_goto(page, home)
        except Exception as exc:
            if browser_closed(exc):
                return {"ok": False, "url": "", "error": "browser_closed"}
            raise
        print(f"Faça login na {label} nesta janela do Google Chrome. A janela fica aberta até 12 minutos.", flush=True)
        deadline = asyncio.get_event_loop().time() + 12 * 60
        href = page.url
        ok = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                href = page.url
                body = ""
                try:
                    body = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                cookies = await context.cookies()
                names = " ".join(str(item.get("name") or "").lower() for item in cookies)
                if any(hint in body for hint in logged_hints) or any(
                    token in names for token in ("token", "auth", "session", "id_token", "access")
                ):
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    session_mark.write_text("ok\n", encoding="utf-8")
                    ok = True
                    print(f"Sessão {label} reconhecida.", flush=True)
                    break
                await page.wait_for_timeout(2000)
            except Exception as exc:
                if browser_closed(exc):
                    return {"ok": ok, "url": href, "error": "browser_closed"}
                await page.wait_for_timeout(2000)
        await context.close()
        return {"ok": ok, "url": href}
