from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.agent import run_search
from app.airports import (
    AIRPORTS,
    CITY_AIRPORTS,
    FEATURED_INTERNATIONAL,
    FEATURED_NATIONAL,
    ORIGIN_LABELS,
    decorate,
    resolve_airport_query,
)
from app.browse import (
    apply_miles_budget,
    browse_flights,
    cheapest_one_ways,
    cheapest_round_trips,
    filter_rows_budget,
    tag_deal_flags,
)
from app.media import program_logo_url
from app.auth import (
    current_user,
    hash_password,
    is_approved,
    normalize_email,
    safe_next_path,
    valid_email,
    verify_password,
)
from app.config import (
    ADMIN_EMAIL,
    APP_PASSWORD,
    CABIN_LABELS,
    DATE_MODES,
    DEFAULT_ORIGINS,
    ENABLE_HARVEST,
    HOST,
    PORT,
    PROGRAM_LABELS,
    ROOT,
    SECRET_KEY,
    SESSION_HTTPS,
    has_live_cia_miles,
    has_miles_provider,
)
from app.db import (
    count_admins,
    count_pending_users,
    count_users,
    create_search,
    create_user,
    get_search,
    get_user_by_email,
    get_user_by_id,
    init_db,
    list_searches,
    list_users,
    now_iso,
    public_user,
    query_results,
    query_route_pair,
    result_stats,
    update_user,
)
from app.dates import format_br_date, format_flight_span, format_updated
from app.excel import build_workbook
from app.harvest import can_live_collect, harvest_scheduler, harvest_status, start_harvest
from app.harvest_plan import jobs_from_request

CODE_RE = re.compile(r"^[A-Z]{3}$")  # IATA de 3 letras


app = FastAPI(title="Viannas pelo Mundo")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="viannas_session",
    same_site="lax",
    https_only=SESSION_HTTPS,
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.globals.update(
    cabin_labels=CABIN_LABELS,
    program_labels=PROGRAM_LABELS,
    default_origins=DEFAULT_ORIGINS,
    origin_labels=ORIGIN_LABELS,
    airport_options=sorted((code, info["city"]) for code, info in AIRPORTS.items()),
    program_logo=program_logo_url,
)


def logged_in(request: Request) -> bool:
    return refresh_user(request) is not None


def refresh_user(request: Request) -> dict[str, Any] | None:
    session_user = current_user(request)
    if not session_user:
        return None
    row = get_user_by_id(int(session_user["id"]))
    if not row:
        request.session.clear()
        return None
    fresh = public_user(row)
    request.session["user"] = fresh
    return fresh


def require_login(request: Request) -> RedirectResponse | None:
    if logged_in(request):
        return None
    nxt = safe_next_path(str(request.url.path))
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


def require_approved(request: Request) -> RedirectResponse | None:
    if redirect := require_login(request):
        return redirect
    if is_approved(refresh_user(request)):
        return None
    return RedirectResponse("/", status_code=303)


def require_admin(request: Request) -> RedirectResponse | None:
    if redirect := require_login(request):
        return redirect
    user = refresh_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/", status_code=303)
    return None


def admin_notice_redirect(aviso: str = "", erro: str = "") -> RedirectResponse:
    if erro:
        return RedirectResponse(f"/admin?erro={quote(erro)}", status_code=303)
    if aviso:
        return RedirectResponse(f"/admin?aviso={quote(aviso)}", status_code=303)
    return RedirectResponse("/admin", status_code=303)


def start_session(request: Request, user: dict[str, Any]) -> None:
    request.session.clear()
    request.session["user"] = public_user(user)


def bootstrap_admin() -> None:
    email = normalize_email(ADMIN_EMAIL)
    if not email or not APP_PASSWORD or count_users():
        return
    create_user("Paloma", email, hash_password(APP_PASSWORD), role="admin", status="approved")


def search_filter_url(search: dict[str, Any] | None) -> str:
    if not search:
        return "/buscar"
    origins = [part.strip() for part in str(search.get("origins") or "").split(",") if part.strip()]
    dests = [part.strip() for part in str(search.get("destinations") or "").split(",") if part.strip()]
    params: dict[str, str] = {"origem": origins[0] if origins else DEFAULT_ORIGINS[0]}
    if len(dests) == 1:
        params["destino"] = dests[0]
    if search.get("trip_type") == "round_trip":
        params["tipo"] = "round_trip"
    if search.get("date_start"):
        params["ida"] = str(search["date_start"])[:10]
    if search.get("date_end"):
        params["volta"] = str(search["date_end"])[:10]
    if search.get("miles_min"):
        params["min"] = str(search["miles_min"])
    if search.get("miles_max"):
        params["max"] = str(search["miles_max"])
    return "/buscar?" + urlencode(params)


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
templates.env.filters["atualizado"] = format_updated


def trip_kind_of(row: dict[str, Any]) -> str:
    kind = row.get("trip_kind")
    if kind in {"ida", "volta", "round_trip"}:
        return kind
    return "round_trip" if row.get("return_date") else "ida"


def tag_best_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return tag_deal_flags(rows)


def destination_cards(origin: str, **filters: Any) -> dict[str, list[dict]]:
    return cheapest_one_ways(origin, **filters)


def parse_miles_input(raw: str | None) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    value = int(digits)
    return value if value > 0 else None


def browse_filters(request: Request) -> dict[str, str]:
    query = request.query_params
    origin = resolve_airport_query(query.get("origem") or query.get("origin") or "")
    destination = resolve_airport_query(query.get("destino") or query.get("destination") or "")
    cabin = query.get("cabin") or query.get("cabine") or "all"
    program = (query.get("cia") or query.get("program") or "").lower()
    if program in {"todas", "all", "*"}:
        program = ""
    miles_min = query.get("min") or query.get("miles_min") or ""
    miles_max = query.get("max") or query.get("miles_max") or ""
    return {
        "origin": origin,
        "destination": destination,
        "trip_type": query.get("tipo") or query.get("trip_type") or "one_way",
        "cabin": cabin if cabin in CABIN_LABELS else "all",
        "program": program if program in {"latam", "azul"} else "",
        "date_from": query.get("ida") or query.get("date_start") or "",
        "date_to": query.get("volta") or query.get("date_end") or "",
        "airline": query.get("companhia") or query.get("airline") or "",
        "miles_min": miles_min,
        "miles_max": miles_max,
    }


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    origin = (
        extra.get("active_origin")
        or request.query_params.get("origem")
        or request.query_params.get("origin")
        or DEFAULT_ORIGINS[0]
    )
    origin = str(origin).upper()
    if origin not in DEFAULT_ORIGINS and origin not in CITY_AIRPORTS:
        origin = DEFAULT_ORIGINS[0]
    filters = extra.get("filters") or browse_filters(request)
    user = refresh_user(request)
    approved = is_approved(user)
    ctx = {
        "request": request,
        "active_origin": origin,
        "active_origin_label": ORIGIN_LABELS.get(origin, origin),
        "origins": DEFAULT_ORIGINS,
        "has_miles": has_miles_provider(),
        "has_live_cia": has_live_cia_miles(),
        "suggested_destinations": ",".join(FEATURED_NATIONAL[:4] + FEATURED_INTERNATIONAL[:4]),
        "nav": extra.get("nav", "home"),
        "harvest": harvest_status(),
        "harvest_live": can_live_collect(),
        "filters": filters,
        "empty_message": extra.get("empty_message") or "",
        "erro": extra.get("erro") or request.query_params.get("erro"),
        "current_user": user,
        "is_admin": bool(user and user.get("role") == "admin"),
        "is_approved": approved,
        "pending_count": count_pending_users() if user and user.get("role") == "admin" else 0,
        "reveal_dates": approved,
        "reveal_national_fares": True,
        "reveal_international_fares": approved,
    }
    ctx.update(extra)
    return ctx


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    bootstrap_admin()
    if not ENABLE_HARVEST:
        return

    async def boot() -> None:
        await harvest_scheduler()

    asyncio.create_task(boot())


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if logged_in(request):
        user = refresh_user(request)
        nxt = safe_next_path(request.query_params.get("next"))
        if not is_approved(user):
            nxt = "/"
        return RedirectResponse(nxt, status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "nav": "login",
            "next": request.query_params.get("next") or "/",
        },
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(...),
    next: str = Form("/"),
):
    nxt = safe_next_path(next)
    email_norm = normalize_email(email)
    user = get_user_by_email(email_norm) if email_norm else None
    if user and user.get("status") == "rejected":
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Essa conta não foi aprovada.",
                "nav": "login",
                "next": nxt,
                "email": email_norm,
            },
            status_code=403,
        )
    if user and verify_password(password, user.get("password_hash")):
        start_session(request, user)
        return RedirectResponse("/" if user.get("status") == "pending" else nxt, status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "E-mail ou senha incorretos.",
            "nav": "login",
            "next": nxt,
            "email": email_norm,
        },
        status_code=401,
    )


@app.get("/conta", response_class=HTMLResponse)
def register_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "error": None,
            "nav": "login",
            "first_user": count_users() == 0,
            "next": request.query_params.get("next") or "/",
        },
    )


@app.post("/conta")
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    nxt = safe_next_path(next)
    name_clean = (name or "").strip()
    email_norm = normalize_email(email)
    error = None
    if len(name_clean) < 2:
        error = "Escreva seu nome."
    elif not valid_email(email_norm):
        error = "E-mail inválido."
    elif len(password) < 8:
        error = "A senha precisa de pelo menos 8 caracteres."
    elif get_user_by_email(email_norm):
        error = "Já existe uma conta com esse e-mail. Entre nela."
    if error:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": error,
                "nav": "login",
                "first_user": count_users() == 0,
                "next": nxt,
                "name": name_clean,
                "email": email_norm,
            },
            status_code=400,
        )
    role = "admin" if count_users() == 0 or email_norm == ADMIN_EMAIL else "viewer"
    status = "approved" if role == "admin" else "pending"
    user = create_user(name_clean, email_norm, hash_password(password), role=role, status=status)
    start_session(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, aviso: str = "", erro: str = ""):
    if redirect := require_admin(request):
        return redirect
    users = list_users()
    return templates.TemplateResponse(
        "admin.html",
        page_context(
            request,
            nav="admin",
            pending_users=[row for row in users if row.get("status") == "pending"],
            approved_users=[
                row for row in users if row.get("status") == "approved" and row.get("role") != "admin"
            ],
            rejected_users=[row for row in users if row.get("status") == "rejected"],
            admin_users=[row for row in users if row.get("role") == "admin"],
            admin_count=count_admins(),
            aviso=aviso,
            erro=erro,
            form_name="",
            form_email="",
        ),
    )


@app.post("/admin/usuarios")
def admin_create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    if redirect := require_admin(request):
        return redirect
    name_clean = (name or "").strip()
    email_norm = normalize_email(email)
    error = None
    if len(name_clean) < 2:
        error = "Escreva o nome da pessoa."
    elif not valid_email(email_norm):
        error = "E-mail inválido."
    elif len(password) < 8:
        error = "A senha precisa de pelo menos 8 caracteres."
    elif get_user_by_email(email_norm):
        error = "Já existe uma conta com esse e-mail."
    if error:
        users = list_users()
        return templates.TemplateResponse(
            "admin.html",
            page_context(
                request,
                nav="admin",
                pending_users=[row for row in users if row.get("status") == "pending"],
                approved_users=[
                    row for row in users if row.get("status") == "approved" and row.get("role") != "admin"
                ],
                rejected_users=[row for row in users if row.get("status") == "rejected"],
                admin_users=[row for row in users if row.get("role") == "admin"],
                admin_count=count_admins(),
                aviso="",
                erro=error,
                form_name=name_clean,
                form_email=email_norm,
            ),
            status_code=400,
        )
    try:
        create_user(name_clean, email_norm, hash_password(password), role="admin", status="approved")
    except sqlite3.IntegrityError:
        return admin_notice_redirect(erro="Já existe uma conta com esse e-mail.")
    return admin_notice_redirect(aviso="Admin cadastrado.")


@app.post("/admin/usuarios/{user_id}/aprovar")
def admin_approve_user(request: Request, user_id: int):
    if redirect := require_admin(request):
        return redirect
    target = get_user_by_id(user_id)
    if not target:
        return admin_notice_redirect(erro="Usuário não encontrado.")
    update_user(user_id, status="approved", approved_at=now_iso())
    return admin_notice_redirect(aviso="Conta aprovada.")


@app.post("/admin/usuarios/{user_id}/recusar")
def admin_reject_user(request: Request, user_id: int):
    if redirect := require_admin(request):
        return redirect
    actor = refresh_user(request)
    target = get_user_by_id(user_id)
    if not target:
        return admin_notice_redirect(erro="Usuário não encontrado.")
    if target.get("role") == "admin":
        if count_admins() <= 1:
            return admin_notice_redirect(erro="Não dá para recusar o último admin.")
        if actor and int(actor["id"]) == int(target["id"]):
            return admin_notice_redirect(erro="Você não pode recusar a própria conta de admin.")
        update_user(user_id, role="viewer", status="rejected", approved_at=None)
        return admin_notice_redirect(aviso="Admin removido e conta recusada.")
    update_user(user_id, status="rejected", approved_at=None)
    return admin_notice_redirect(aviso="Conta recusada.")


@app.post("/admin/usuarios/{user_id}/admin")
def admin_promote_user(request: Request, user_id: int):
    if redirect := require_admin(request):
        return redirect
    target = get_user_by_id(user_id)
    if not target:
        return admin_notice_redirect(erro="Usuário não encontrado.")
    update_user(user_id, role="admin", status="approved", approved_at=target.get("approved_at") or now_iso())
    return admin_notice_redirect(aviso="Essa pessoa agora é admin.")


@app.post("/admin/usuarios/{user_id}/tirar-admin")
def admin_demote_user(request: Request, user_id: int):
    if redirect := require_admin(request):
        return redirect
    actor = refresh_user(request)
    target = get_user_by_id(user_id)
    if not target or target.get("role") != "admin":
        return admin_notice_redirect(erro="Essa pessoa já não é admin.")
    if count_admins() <= 1:
        return admin_notice_redirect(erro="Precisa ficar pelo menos um admin.")
    if actor and int(actor["id"]) == int(target["id"]):
        return admin_notice_redirect(erro="Peça para outro admin tirar o seu acesso.")
    update_user(user_id, role="viewer", status="approved")
    return admin_notice_redirect(aviso="Essa pessoa deixou de ser admin e continua com a conta aprovada.")


def _coleta_denied() -> RedirectResponse:
    return RedirectResponse(
        "/?erro=" + quote("A coleta com Chrome só roda neste computador, não no Vercel."),
        status_code=303,
    )


@app.post("/coleta")
async def trigger_harvest(
    request: Request,
    modo: str = Form(""),
    regiao: str = Form(""),
    escopo: str = Form(""),
    alvo: str = Form(""),
):
    if redirect := require_admin(request):
        return redirect
    if not can_live_collect():
        return _coleta_denied()
    scope = (escopo or "").strip().lower()
    target = (alvo or "").strip()
    region = (regiao or "").strip() or None
    kind = None
    if scope in {"region", "country", "state", "airport"} and target:
        search_id = start_harvest("pedido", scope=scope, target=target)
        return RedirectResponse(f"/buscas/{search_id}", status_code=303)
    if not region:
        token = (modo or "").strip().lower()
        if token in {"nacional", "nacionais", "national"}:
            kind = "national"
        elif token in {"internacional", "internacionais", "international"}:
            kind = "international"
        elif token in {"ambos", "both"}:
            kind = "both"
    search_id = start_harvest("pedido", kind=kind, region=region)
    return RedirectResponse(f"/buscas/{search_id}", status_code=303)


@app.post("/buscar/coletar")
async def collect_from_search(
    request: Request,
    origem: str = Form(""),
    destino: str = Form(""),
    tipo: str = Form("one_way"),
    cabin: str = Form("all"),
    ida: str = Form(""),
    volta: str = Form(""),
    cia: str = Form(""),
    companhia: str = Form(""),
    min: str = Form(""),
    max: str = Form(""),
):
    if redirect := require_admin(request):
        return redirect
    if not can_live_collect():
        return _coleta_denied()
    dest = (destino or "").strip()
    if not dest:
        return RedirectResponse(
            "/buscar?erro=" + quote("Preencha o destino para o Chrome buscar na cia."),
            status_code=303,
        )
    origin = (origem or DEFAULT_ORIGINS[0]).strip().upper()
    trip_type = tipo if tipo in {"one_way", "round_trip"} else "one_way"
    program = (cia or "").strip().lower()
    programs = [program] if program in {"latam", "azul"} else ["latam"]
    jobs = jobs_from_request(origin, dest, trip_type, ida or None, volta or None, programs)
    if not jobs:
        return RedirectResponse(
            "/buscar?erro=" + quote("Não achei esse destino no catálogo para coletar."),
            status_code=303,
        )
    search_id = start_harvest(
        "pedido",
        jobs=jobs,
        payload={
            "origins": sorted({job["origin"] for job in jobs}),
            "destinations": sorted({job["destination"] for job in jobs}),
            "trip_type": trip_type,
            "date_mode": "specific",
            "date_start": jobs[0].get("day"),
            "date_end": jobs[0].get("return_day"),
            "stay_nights": 7,
            "cabin": cabin if cabin in CABIN_LABELS else "economy",
            "include_cash": False,
            "include_miles": True,
            "programs": programs,
            "miles_min": parse_miles_input(min),
            "miles_max": parse_miles_input(max),
        },
    )
    return RedirectResponse(f"/buscas/{search_id}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = page_context(request, searches=list_searches() if is_approved(refresh_user(request)) else [], nav="home", page_mode="ida")
    cards = destination_cards(ctx["active_origin"])
    if not ctx["is_approved"]:
        cards = {
            "national": cards["national"][:6],
            "international": cards["international"][:3],
            "all": cards["national"][:6] + cards["international"][:3],
        }
        ctx["reveal_dates"] = False
        ctx["reveal_international_fares"] = False
    ctx["cards"] = cards
    return templates.TemplateResponse("home.html", ctx)


@app.get("/ida", response_class=HTMLResponse)
def one_way_page(request: Request):
    if redirect := require_approved(request):
        return redirect
    ctx = page_context(request, nav="ida")
    ctx["filters"] = {**ctx["filters"], "trip_type": "one_way"}
    ctx["cards"] = destination_cards(ctx["active_origin"])
    ctx["page_mode"] = "ida"
    return templates.TemplateResponse("ida.html", ctx)


@app.get("/ida-volta", response_class=HTMLResponse)
def round_page(request: Request):
    if redirect := require_approved(request):
        return redirect
    ctx = page_context(request, nav="round")
    ctx["filters"] = {**ctx["filters"], "trip_type": "round_trip"}
    ctx["cards"] = cheapest_round_trips(ctx["active_origin"])
    ctx["page_mode"] = "round"
    return templates.TemplateResponse("round.html", ctx)


@app.get("/buscar", response_class=HTMLResponse)
def search_page(request: Request):
    if redirect := require_approved(request):
        return redirect
    filters = browse_filters(request)
    origin = filters["origin"] or (request.query_params.get("origem") or DEFAULT_ORIGINS[0]).upper()
    extra_filters = {
        "cabin": filters["cabin"],
        "program": filters["program"] or None,
        "date_from": filters["date_from"] or None,
        "date_to": filters["date_to"] or None,
        "airline": filters["airline"] or None,
    }
    trip_type = filters["trip_type"] if filters["trip_type"] in {"one_way", "round_trip"} else "one_way"
    miles_min = parse_miles_input(filters.get("miles_min"))
    miles_max = parse_miles_input(filters.get("miles_max"))
    if trip_type == "round_trip":
        raw_cards = cheapest_round_trips(origin, filters["destination"] or None, **extra_filters)
        cards = apply_miles_budget(raw_cards, origin, trip_type, miles_min, miles_max)
        rows = cards["all"]
    else:
        raw_cards = cheapest_one_ways(origin, filters["destination"] or None, **extra_filters)
        cards = apply_miles_budget(raw_cards, origin, trip_type, miles_min, miles_max)
        rows = filter_rows_budget(
            browse_flights(
                origin,
                filters["destination"] or None,
                "one_way",
                **extra_filters,
            ),
            origin,
            trip_type,
            miles_min,
            miles_max,
        )
        rows = tag_best_prices(rows)
    searched = any(
        request.query_params.get(key)
        for key in ("destino", "ida", "volta", "min", "max", "cia", "companhia", "tipo")
    )
    empty_message = ""
    if searched and not cards["all"]:
        if raw_cards["all"]:
            empty_message = (
                "Não encontramos valores ida e volta dentro dos valores determinados"
                if trip_type == "round_trip"
                else "Não encontramos valores de ida dentro dos valores determinados"
            )
        else:
            empty_message = "Ainda não há milhas coletadas para este trecho."
    ctx = page_context(
        request,
        nav="search",
        active_origin=origin,
        filters=filters,
        cards=cards,
        rows=rows,
        page_mode="round" if trip_type == "round_trip" else "ida",
        empty_message=empty_message,
        stats=result_stats(
            price_type="miles",
            origin=origin or None,
            destination=filters["destination"] or None,
        ),
    )
    return templates.TemplateResponse("buscar.html", ctx)


@app.get("/dinheiro")
def money_page(request: Request):
    if redirect := require_approved(request):
        return redirect
    return RedirectResponse("/milhas", status_code=303)


@app.get("/milhas", response_class=HTMLResponse)
def miles_page(
    request: Request,
    origin: str = "",
    destination: str = "",
    sort: str = "miles",
    trecho: str = "",
):
    if redirect := require_approved(request):
        return redirect
    origin_code = (origin or request.query_params.get("origem") or "").upper()
    dest_code = (destination or "").upper()
    trip_kind = trecho if trecho in {"ida", "volta", "round_trip"} else None
    raw_rows = (
        query_route_pair(
            origin_code,
            dest_code,
            price_type="miles",
            trip_kind=trip_kind,
            sort=sort,
        )
        if origin_code and dest_code
        else query_results(
            origin=origin_code or None,
            destination=dest_code or None,
            price_type="miles",
            trip_kind=trip_kind,
            sort=sort,
        )
    )
    rows = tag_best_prices([decorate(row) for row in raw_rows])
    stats = result_stats(
        price_type="miles",
        origin=origin_code or None,
        destination=dest_code or None,
    )
    if origin_code and dest_code:
        inbound_stats = result_stats(
            price_type="miles",
            origin=dest_code,
            destination=origin_code,
        )
        stats = {
            "total": (stats.get("total") or 0) + (inbound_stats.get("total") or 0),
            "cash_count": (stats.get("cash_count") or 0) + (inbound_stats.get("cash_count") or 0),
            "miles_count": (stats.get("miles_count") or 0) + (inbound_stats.get("miles_count") or 0),
            "min_cash": stats.get("min_cash"),
            "min_miles": min(
                value
                for value in (stats.get("min_miles"), inbound_stats.get("min_miles"))
                if value is not None
            )
            if stats.get("min_miles") is not None or inbound_stats.get("min_miles") is not None
            else None,
        }
    return templates.TemplateResponse(
        "results.html",
        page_context(
            request,
            nav="search",
            active_origin=origin_code or DEFAULT_ORIGINS[0],
            search=None,
            rows=rows,
            stats=stats,
            sort=sort,
            scope="miles",
            can_combine=bool(origin_code and dest_code),
            cards=destination_cards(origin_code or DEFAULT_ORIGINS[0]),
            filters={
                "origin": origin_code,
                "destination": dest_code,
                "trip_type": "one_way",
                "cabin": "all",
                "program": "",
                "date_from": "",
                "date_to": "",
                "airline": "",
                "trecho": trip_kind or "",
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
    if redirect := require_admin(request):
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
        programs = ["latam"]
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
    if redirect := require_approved(request):
        return redirect
    search = get_search(search_id)
    if not search:
        return RedirectResponse("/", status_code=303)
    rows = tag_best_prices(
        [decorate(row) for row in query_results(search_id=search_id, price_type="miles", sort=sort)]
    )
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
            can_combine=False,
            filters={"origin": "", "destination": "", "trecho": ""},
            browse_url=search_filter_url(search),
        ),
    )


@app.get("/buscas/{search_id}/status")
def search_status(request: Request, search_id: int):
    if not is_approved(refresh_user(request)):
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
    if redirect := require_approved(request):
        return redirect
    if origin and destination:
        rows = query_route_pair(
            origin,
            destination,
            price_type="miles",
            cabin=cabin or None,
            sort=sort,
            limit=5000,
        )
    else:
        rows = query_results(
            search_id=search_id,
            origin=origin or None,
            destination=destination or None,
            price_type="miles",
            cabin=cabin or None,
            sort=sort,
            limit=5000,
        )
    rows = tag_best_prices(rows)
    filename = f"passagens-{search_id or price_type or 'todas'}.xlsx"
    return Response(
        content=build_workbook(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
