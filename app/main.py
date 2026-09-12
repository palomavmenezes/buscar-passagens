from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.agent import run_search
from app.airports import (
    FEATURED_INTERNATIONAL,
    FEATURED_NATIONAL,
    decorate,
)
from app.config import (
    APP_PASSWORD,
    CABIN_LABELS,
    DATE_MODES,
    DEFAULT_ORIGINS,
    HOST,
    PORT,
    PROGRAM_LABELS,
    ROOT,
    SECRET_KEY,
    has_live_cia_miles,
    has_miles_provider,
)
from app.db import (
    cheapest_destinations,
    create_search,
    get_search,
    init_db,
    list_searches,
    query_results,
    result_stats,
)
from app.dates import format_br_date, format_flight_span
from app.excel import build_workbook
from app.harvest import harvest_scheduler, harvest_status, run_harvest, slot_for_now

CODE_RE = re.compile(r"^[A-Z]{3}$")

app = FastAPI(title="Viannas pelo Mundo")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="viannas_session")
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.globals.update(
    cabin_labels=CABIN_LABELS,
    program_labels=PROGRAM_LABELS,
    default_origins=DEFAULT_ORIGINS,
)


def logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_login(request: Request) -> RedirectResponse | None:
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


def parse_codes(raw: str, fallback: list[str] | None = None) -> list[str]:
    codes = []
    for token in re.split(r"[\s,;]+", (raw or "").upper()):
        if CODE_RE.match(token) and token not in codes:
            codes.append(token)
    if not codes and fallback:
        return list(fallback)
    return codes


def format_money(value) -> str:
    if value in (None, ""):
        return "—"
    return f"R$ {float(value):,.0f}".replace(",", ".")


def format_miles(value) -> str:
    if value in (None, ""):
        return "—"
    return f"{int(value):,}".replace(",", ".")


templates.env.filters["brl"] = format_money
templates.env.filters["milhas"] = format_miles
templates.env.filters["brdate"] = format_br_date
templates.env.filters["voo"] = format_flight_span


def destination_cards(origin: str) -> dict[str, list[dict]]:
    cards: list[dict] = []
    for row in cheapest_destinations(origin):
        if not row.get("miles"):
            continue
        base = decorate(row)
        base["miles"] = row.get("miles")
        base["miles_program"] = row.get("miles_program")
        base["miles_date"] = row.get("departure_date")
        base["miles_time"] = row.get("departure_time")
        base["miles_arrival"] = row.get("arrival_time")
        base["miles_return"] = row.get("return_date")
        cards.append(base)
    cards.sort(key=lambda item: item.get("miles") or 0)
    return {
        "national": [card for card in cards if card["national"]],
        "international": [card for card in cards if not card["national"]],
    }


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    origin = (
        request.query_params.get("origem")
        or request.query_params.get("origin")
        or DEFAULT_ORIGINS[0]
    ).upper()
    if origin not in DEFAULT_ORIGINS:
        origin = DEFAULT_ORIGINS[0]
    ctx = {
        "request": request,
        "active_origin": origin,
        "origins": DEFAULT_ORIGINS,
        "has_miles": has_miles_provider(),
        "has_live_cia": has_live_cia_miles(),
        "cards": destination_cards(origin),
        "suggested_destinations": ",".join(FEATURED_NATIONAL[:4] + FEATURED_INTERNATIONAL[:4]),
        "nav": extra.get("nav", "home"),
        "harvest": harvest_status(),
    }
    ctx.update(extra)
    return ctx


@app.on_event("startup")
def on_startup() -> None:
    init_db()

    async def boot() -> None:
        await harvest_scheduler()

    asyncio.create_task(boot())


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "nav": "login"})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password != APP_PASSWORD:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Senha incorreta.", "nav": "login"},
            status_code=401,
        )
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/coleta")
async def trigger_harvest(request: Request):
    if redirect := require_login(request):
        return redirect
    result = await run_harvest(slot_for_now())
    search_id = result.get("search_id")
    if search_id:
        return RedirectResponse(f"/buscas/{search_id}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if redirect := require_login(request):
        return redirect
    return templates.TemplateResponse(
        "home.html",
        page_context(request, searches=list_searches(), nav="home"),
    )


@app.get("/dinheiro")
def money_page(request: Request):
    if redirect := require_login(request):
        return redirect
    return RedirectResponse("/milhas", status_code=303)


@app.get("/milhas", response_class=HTMLResponse)
def miles_page(
    request: Request,
    origin: str = "",
    destination: str = "",
    sort: str = "miles",
):
    if redirect := require_login(request):
        return redirect
    origin_code = (origin or request.query_params.get("origem") or "").upper()
    rows = [
        decorate(row)
        for row in query_results(
            origin=origin_code or None,
            destination=destination or None,
            price_type="miles",
            sort=sort,
        )
    ]
    return templates.TemplateResponse(
        "results.html",
        page_context(
            request,
            nav="miles",
            search=None,
            rows=rows,
            stats=result_stats(
                price_type="miles",
                origin=origin_code or None,
                destination=destination or None,
            ),
            sort=sort,
            scope="miles",
            filters={
                "origin": origin_code,
                "destination": destination.upper(),
                "price_type": "miles",
                "cabin": "",
            },
        ),
    )


@app.post("/buscas")
async def create_busca(
    request: Request,
    origins: str = Form(...),
    destinations: str = Form(...),
    trip_type: str = Form("one_way"),
    date_mode: str = Form("flex"),
    date_start: str = Form(""),
    date_end: str = Form(""),
    stay_nights: int = Form(7),
    cabin: str = Form("economy"),
    kind: str = Form("miles"),
    smiles: str | None = Form(None),
    azul: str | None = Form(None),
    latam: str | None = Form(None),
):
    if redirect := require_login(request):
        return redirect
    origin_codes = parse_codes(origins, DEFAULT_ORIGINS)
    dest_codes = parse_codes(destinations)
    if not dest_codes:
        dest_codes = FEATURED_NATIONAL[:4] + FEATURED_INTERNATIONAL[:4]
    programs = [
        name
        for name, flag in (("smiles", smiles), ("azul", azul), ("latam", latam))
        if flag == "on"
    ]
    if not programs:
        programs = ["smiles", "azul", "latam"]
    payload: dict[str, Any] = {
        "origins": origin_codes,
        "destinations": dest_codes,
        "trip_type": trip_type if trip_type in {"one_way", "round_trip"} else "one_way",
        "date_mode": date_mode if date_mode in DATE_MODES else "flex",
        "date_start": date_start or None,
        "date_end": date_end or None,
        "stay_nights": max(1, min(stay_nights, 30)),
        "cabin": cabin if cabin in CABIN_LABELS else "economy",
        "include_cash": False,
        "include_miles": True,
        "programs": programs,
        "sample_step": 1 if date_mode == "specific" else 7,
    }
    search_id = create_search(payload)
    asyncio.create_task(run_search(search_id, payload))
    return RedirectResponse(f"/buscas/{search_id}", status_code=303)


@app.get("/buscas/{search_id}", response_class=HTMLResponse)
def search_detail(request: Request, search_id: int, sort: str = "cheapest"):
    if redirect := require_login(request):
        return redirect
    search = get_search(search_id)
    if not search:
        return RedirectResponse("/", status_code=303)
    rows = [decorate(row) for row in query_results(search_id=search_id, price_type="miles", sort=sort)]
    return templates.TemplateResponse(
        "results.html",
        page_context(
            request,
            nav="miles",
            search=search,
            rows=rows,
            stats=result_stats(search_id, price_type="miles"),
            sort=sort,
            scope="search",
        ),
    )


@app.get("/buscas/{search_id}/status")
def search_status(request: Request, search_id: int):
    if not logged_in(request):
        return {"ok": False}
    search = get_search(search_id)
    if not search:
        return {"ok": False}
    return {
        "ok": True,
        "status": search["status"],
        "progress": search["progress"],
        "error": search["error"],
    }


@app.get("/export.xlsx")
def export_xlsx(
    request: Request,
    search_id: int | None = None,
    origin: str = "",
    destination: str = "",
    price_type: str = "miles",
    cabin: str = "",
    sort: str = "cheapest",
):
    if redirect := require_login(request):
        return redirect
    rows = query_results(
        search_id=search_id,
        origin=origin or None,
        destination=destination or None,
        price_type="miles",
        cabin=cabin or None,
        sort=sort,
        limit=5000,
    )
    filename = f"passagens-{search_id or price_type or 'todas'}.xlsx"
    return Response(
        content=build_workbook(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
