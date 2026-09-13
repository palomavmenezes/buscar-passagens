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
from app.collectors.human import (
    browse_results,
    host_page,
    human_click,
    human_pause,
    human_type,
    rest_like_a_person,
    wander_mouse,
)
from app.config import CIA_HEADLESS, CIA_PAUSE_SECONDS, DATA_DIR, airline_credentials, harvest_route_path
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
    await human_pause(page, low, high)


async def rest_on_results_then_home(page, home: str, seconds: float) -> bool:
    return await rest_like_a_person(page, home, seconds, safe_goto)


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
    host = host_page(root)
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in roles:
            target = await first_visible(root.get_by_role(role, name=pattern), timeout=700)
            if target is None:
                continue
            if await human_click(target, page=host, timeout=5000):
                return label
    return None


LOGIN_OPEN = {
    "latam": (
        "iniciar sessão",
        "inicie sessão",
        "faça seu login",
        "faca seu login",
        "^fazer login$",
        "entrar na minha conta",
    ),
    "azul": ("olá, faça login", "ola, faca login"),
    "smiles": ("entrar", "acessar", "login"),
}
LOGGED_BY_PROGRAM = {
    "latam": ("encerrar sessão", "cerrar sesión", "minha conta"),
    "azul": (
        "saldo de pontos",
        "você está no nível",
        "voce esta no nivel",
        "consultar extrato",
        "crédito azul",
        "credito azul",
        "sair da conta",
        "encerrar sessão",
    ),
    "smiles": ("encerrar sessão", "meus dados", "minha conta"),
}
AZUL_ACCOUNT_JS = r"""() => {
  const inHeader = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8 && r.top >= 0 && r.top < 160 && r.left > window.innerWidth * 0.4;
  };
  let chip = '';
  let loggedOut = false;
  for (const el of document.querySelectorAll('button, a, [role="button"]')) {
    if (!inHeader(el)) continue;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const aria = (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const blob = `${text} ${aria}`;
    if (/olá,?\s*faça login|ola,?\s*faca login/i.test(blob)) {
      loggedOut = true;
      continue;
    }
    if (/^[A-ZÀ-Ÿ]{1,3}\s+[A-Za-zÀ-ÿ']{3,}/.test(text)) chip = text;
  }
  const body = (document.body && document.body.innerText || '').toLowerCase();
  const menuHits = [
    'saldo de pontos',
    'você está no nível',
    'voce esta no nivel',
    'consultar extrato',
    'crédito azul',
    'credito azul',
  ].filter((hint) => body.includes(hint)).length;
  if (chip || menuHits >= 2) return 'logged_in';
  if (loggedOut) return 'logged_out';
  return 'unknown';
}"""
AZUL_ACCOUNT_CLICK_JS = r"""() => {
  const inHeader = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8 && r.top >= 0 && r.top < 160 && r.left > window.innerWidth * 0.4;
  };
  let chip = null;
  let loggedOut = null;
  for (const el of document.querySelectorAll('button, a, [role="button"]')) {
    if (!inHeader(el)) continue;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const aria = (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (/olá,?\s*faça login|ola,?\s*faca login/i.test(`${text} ${aria}`)) loggedOut = el;
    else if (/^[A-ZÀ-Ÿ]{1,3}\s+[A-Za-zÀ-ÿ']{3,}/.test(text)) chip = el;
  }
  const target = chip || loggedOut;
  if (!target) return '';
  target.click();
  return chip ? 'chip' : 'logged_out';
}"""
LOGGED_OUT_HINTS = {
    "latam": ("faça seu login", "faca seu login", "fazer login"),
    "azul": ("olá, faça login", "ola, faca login"),
    "smiles": ("faça login", "fazer login"),
}


def _program_from_label(label: str) -> str:
    low = (label or "").lower()
    if "azul" in low:
        return "azul"
    if "smiles" in low or "gol" in low:
        return "smiles"
    return "latam"


USER_FIELD_LABELS = (
    "e-mail",
    "email",
    "cpf",
    "usuário",
    "usuario",
    "latam pass",
    "número latam",
    "numero latam",
    "documento",
    "username",
)
PASSWORD_FIELD_LABELS = ("senha", "password", "contraseña")
CONTINUE_LABELS = ("continuar", "próximo", "proximo", "entrar", "iniciar sessão", "^fazer login$", "continue")


def _login_href(page) -> str:
    return (getattr(page, "url", None) or "").lower()


async def _login_body(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


def _on_login_page(href: str, body: str = "") -> bool:
    _ = body
    return any(token in href for token in ("/u/login", "/login", "/signin", "auth.", "sso.", "identidade"))


def _looks_logged_out_cta(body: str, program: str) -> bool:
    return any(hint in (body or "") for hint in LOGGED_OUT_HINTS.get(program, ()))


async def _login_form_visible(page) -> bool:
    try:
        if await first_visible(page.locator('input[type="password"]'), timeout=700):
            return True
    except Exception:
        pass
    return False


async def _latam_login_gate_visible(page) -> bool:
    try:
        popper = page.locator('#login-incentive-popper, [data-testid="login-incentive-popper--card"]')
        if await popper.count() and await popper.first.is_visible(timeout=600):
            return True
    except Exception:
        pass
    try:
        header = await first_visible(
            page.get_by_role("button", name=re.compile(r"^fazer login$", re.I)),
            timeout=500,
        )
        if header is not None:
            return True
    except Exception:
        pass
    return False


async def _open_latam_login_ui(page) -> bool:
    await dismiss_banners(page)
    popper = page.locator('#login-incentive-popper, [data-testid="login-incentive-popper--card"]')
    try:
        btn = await first_visible(
            popper.get_by_role("button", name=re.compile(r"^fazer login$", re.I)),
            timeout=800,
        )
        if btn is not None:
            try:
                await btn.click()
            except Exception:
                await btn.click(force=True)
            return True
    except Exception:
        pass
    opened = await click_named(
        page,
        LOGIN_OPEN["latam"],
        roles=("button", "link"),
    )
    return bool(opened)


async def _logged_out_control_visible(page, program: str) -> bool:
    labels = {
        "latam": ("iniciar sessão", "inicie sessão", "faça seu login", "^fazer login$"),
        "azul": ("olá, faça login", "ola, faca login"),
        "smiles": ("fazer login",),
    }.get(program, ())
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in ("button", "link"):
            try:
                if await first_visible(page.get_by_role(role, name=pattern), timeout=500):
                    return True
            except Exception:
                continue
    return False


async def _azul_session_state(page) -> str:
    try:
        state = await page.evaluate(AZUL_ACCOUNT_JS)
    except Exception:
        return "unknown"
    return state if state in {"logged_in", "logged_out"} else "unknown"


async def _azul_account_menu_open(page) -> bool:
    body = await _login_body(page)
    hits = sum(
        1
        for hint in (
            "saldo de pontos",
            "você está no nível",
            "voce esta no nivel",
            "consultar extrato",
            "crédito azul",
            "credito azul",
        )
        if hint in body
    )
    return hits >= 2


async def _click_azul_account_chip(page) -> str:
    try:
        return str(await page.evaluate(AZUL_ACCOUNT_CLICK_JS) or "")
    except Exception:
        return ""


async def dismiss_account_overlay(page) -> None:
    if not await _azul_account_menu_open(page):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(350)
    if await _azul_account_menu_open(page):
        await _click_azul_account_chip(page)
        await page.wait_for_timeout(350)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    if await _azul_account_menu_open(page):
        origem = await first_visible(
            page.get_by_role("combobox", name=re.compile(r"^origem$", re.I)),
            timeout=600,
        )
        if origem is not None:
            try:
                await origem.click()
                await page.keyboard.press("Escape")
            except Exception:
                pass


def _mark_session(session_mark: Path | None) -> None:
    if not session_mark:
        return
    session_mark.parent.mkdir(parents=True, exist_ok=True)
    session_mark.write_text("ok\n", encoding="utf-8")


async def _azul_ready_to_search(page, session_mark: Path | None) -> bool | None:
    if _on_login_page(_login_href(page)) or await _login_form_visible(page):
        return None
    state = await _azul_session_state(page)
    if state == "logged_out":
        return None
    if state == "unknown":
        print("Abro o menu da conta Azul só para conferir se já estou logada.", flush=True)
        await _click_azul_account_chip(page)
        await page.wait_for_timeout(900)
        if await _login_form_visible(page):
            return None
        state = await _azul_session_state(page)
        if state == "logged_out":
            return None
    await dismiss_account_overlay(page)
    _mark_session(session_mark)
    print("Já estou logada na Azul. Fecho o menu da conta e preencho a busca.", flush=True)
    return True


async def already_logged(page, logged_hints: tuple[str, ...], program: str = "") -> bool:
    href = _login_href(page)
    if _on_login_page(href):
        return False
    if await _login_form_visible(page):
        return False
    if program == "azul":
        state = await _azul_session_state(page)
        if state == "logged_in":
            return True
        if state == "logged_out":
            return False
    body = await _login_body(page)
    if program != "azul" and _looks_logged_out_cta(body, program):
        return False
    return any(hint in body for hint in logged_hints)


async def _page_has_challenge(page) -> str | None:
    body = await _login_body(page)
    if any(
        hint in body
        for hint in (
            "código de verificação",
            "codigo de verificacao",
            "two-factor",
            "autenticador",
            "enviamos um código",
            "enviamos um codigo",
            "verifique seu celular",
            "como receber o código",
            "como receber o codigo",
            "pelo whatsapp",
            "por whatsapp",
            "via whatsapp",
            "no whatsapp",
            "digite o código",
            "digite o codigo",
            "código enviado",
            "codigo enviado",
            "verificação em duas etapas",
            "verificacao em duas etapas",
        )
    ):
        return "2fa"
    if "não sou um robô" in body or "nao sou um robo" in body or "complete the captcha" in body:
        return "captcha"
    return None


async def _wait_for_human_challenge(
    page,
    program: str,
    session_mark: Path | None,
    logged_hints: tuple[str, ...],
    home: str,
    timeout_s: float = 20 * 60,
) -> bool:
    kind = await _page_has_challenge(page) or "2fa"
    pedido = "captcha" if kind == "captcha" else "código de e-mail ou WhatsApp"
    print(
        f"A {program.upper()} pediu {pedido}. Deixo o Chrome aberto. "
        "Quando o código chegar, preencha aí. Eu espero até 20 minutos.",
        flush=True,
    )
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_ping = 0.0
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(2000)
        href = _login_href(page)
        left_challenge = not await _page_has_challenge(page) and not _on_login_page(href)
        if await already_logged(page, logged_hints, program) or (
            left_challenge and "latamairlines.com" in href
        ):
            _mark_session(session_mark)
            if home:
                try:
                    await safe_goto(page, home)
                except Exception:
                    pass
            print(f"Login da {program.upper()} feito. Sigo a busca.", flush=True)
            return True
        now = asyncio.get_event_loop().time()
        if now - last_ping >= 30:
            left = int(deadline - now)
            print(f"Chrome aberto; ainda espero você colocar o código ({left}s).", flush=True)
            last_ping = now
    print(
        f"Passei 20 min e o código da {program.upper()} não confirmou a sessão.",
        flush=True,
    )
    return False


async def _find_login_field(root, labels: tuple[str, ...], kinds: tuple[str, ...] = ("textbox", "searchbox")):
    for label in labels:
        pattern = re.compile(label, re.I)
        for role in kinds:
            field = await first_visible(root.get_by_role(role, name=pattern), timeout=700)
            if field is not None:
                return field
        field = await first_visible(root.get_by_placeholder(pattern), timeout=500)
        if field is not None:
            return field
        field = await first_visible(root.get_by_label(pattern), timeout=500)
        if field is not None:
            return field
    return None


async def _fill_login_field(field, value: str) -> None:
    await human_type(field.page, field, value)


async def _submit_login(root) -> str | None:
    clicked = await click_named(root, CONTINUE_LABELS, roles=("button", "link"))
    if clicked:
        return clicked
    try:
        submit = root.locator('button[type="submit"], input[type="submit"]')
        target = await first_visible(submit, timeout=800)
        if target is not None:
            await human_click(target, timeout=4000)
            return "submit"
    except Exception:
        pass
    return None


async def login_with_env(page, program: str, home: str, session_mark: Path | None = None) -> bool:
    """Só preenche usuário/senha se a sessão estiver fora. Se já estiver logado, não mexe."""
    program_key = (program or "").strip().lower()
    if program_key == "latam":
        await dismiss_banners(page)
    if program_key == "azul":
        ready = await _azul_ready_to_search(page, session_mark)
        if ready is not None:
            return ready
    user, password = airline_credentials(program_key)
    logged_hints = LOGGED_BY_PROGRAM.get(program_key) or LOGGED_BY_PROGRAM["latam"]
    href = _login_href(page)
    body = await _login_body(page)
    on_login = _on_login_page(href)
    form_open = await _login_form_visible(page)
    logged_in = await already_logged(page, logged_hints, program_key)
    latam_gate = program_key == "latam" and await _latam_login_gate_visible(page)
    logged_out = (
        on_login
        or form_open
        or latam_gate
        or (program_key != "azul" and _looks_logged_out_cta(body, program_key))
        or await _logged_out_control_visible(page, program_key)
    )

    if logged_in and not logged_out:
        _mark_session(session_mark)
        print(f"Já estou logado na {program_key.upper()}. Não preencho login.", flush=True)
        return True
    if not logged_out:
        print(f"Não estou deslogado na {program_key.upper()}; sigo sem abrir login.", flush=True)
        return True
    if not user or not password:
        print(
            f"Sem {program_key.upper()}_USER / {program_key.upper()}_PASSWORD no .env; não entro sozinho.",
            flush=True,
        )
        return False
    challenge = await _page_has_challenge(page)
    if challenge:
        return await _wait_for_human_challenge(page, program_key, session_mark, logged_hints, home)
    if not on_login and not form_open:
        if program_key == "latam":
            opened = await _open_latam_login_ui(page)
        else:
            opened = await click_named(
                page,
                LOGIN_OPEN.get(program_key, LOGIN_OPEN["latam"]),
                roles=("button", "link"),
            )
        if opened:
            print(f"Sessão da {program_key.upper()} estava fora. Abro o login.", flush=True)
            await page.wait_for_timeout(2500)
    challenge = await _page_has_challenge(page)
    if challenge:
        return await _wait_for_human_challenge(page, program_key, session_mark, logged_hints, home)
    print(f"Preencho o login da {program_key.upper()} com o .env.", flush=True)
    filled_user = False
    filled_password = False
    for _attempt in range(4):
        roots = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        for root in roots:
            try:
                user_box = await _find_login_field(root, USER_FIELD_LABELS)
                if user_box is None:
                    user_box = await first_visible(
                        root.locator(
                            'input#username, input[name="username"], input[type="email"], '
                            'input[autocomplete="username"], input[name="email"]'
                        ),
                        timeout=600,
                    )
                if user_box is not None and not filled_user:
                    await _fill_login_field(user_box, user)
                    filled_user = True
                    await page.wait_for_timeout(600)
                password_box = await _find_login_field(root, PASSWORD_FIELD_LABELS)
                if password_box is None:
                    password_box = await first_visible(root.locator('input[type="password"]'), timeout=700)
                if password_box is None and filled_user:
                    await _submit_login(root)
                    await page.wait_for_timeout(1500)
                    password_box = await first_visible(root.locator('input[type="password"]'), timeout=1500)
                if password_box is not None:
                    await _fill_login_field(password_box, password)
                    filled_password = True
                    await _submit_login(root)
                    break
            except Exception:
                continue
        if filled_password:
            break
        await page.wait_for_timeout(1200)
    if not filled_user:
        print(f"Não achei o campo de usuário da {program_key.upper()}.", flush=True)
        if await _page_has_challenge(page):
            return await _wait_for_human_challenge(page, program_key, session_mark, logged_hints, home)
        await page.wait_for_timeout(4000)
        if await _page_has_challenge(page):
            return await _wait_for_human_challenge(page, program_key, session_mark, logged_hints, home)
        return False
    deadline = asyncio.get_event_loop().time() + 45
    while asyncio.get_event_loop().time() < deadline:
        challenge = await _page_has_challenge(page)
        if challenge:
            return await _wait_for_human_challenge(page, program_key, session_mark, logged_hints, home)
        body = await _login_body(page)
        if any(err in body for err in ("senha incorreta", "usuário não reconhecido", "usuario no reconocido", "e-mail ou senha")):
            print(f"A {program_key.upper()} recusou o login. Confira usuário e senha no .env.", flush=True)
            return False
        if await already_logged(page, logged_hints, program_key):
            _mark_session(session_mark)
            if program_key == "azul":
                await dismiss_account_overlay(page)
            if home and home not in (page.url or ""):
                try:
                    await safe_goto(page, home)
                except Exception:
                    pass
            print(f"Login da {program_key.upper()} feito.", flush=True)
            return True
        await page.wait_for_timeout(1500)
    print(f"O login da {program_key.upper()} não confirmou a sessão a tempo.", flush=True)
    return False


def _is_continue_search(name: str) -> bool:
    return bool(re.search(r"continuar busca", name or "", re.I))


async def click_home_search(page, labels: tuple[str, ...]) -> str | None:
    found = await click_named(page, labels)
    if found and not _is_continue_search(found):
        return found
    loc = page.get_by_role(
        "button",
        name=re.compile(r"buscar passagens|buscar voos|procurar voos|pesquisar voos", re.I),
    )
    try:
        count = await loc.count()
    except Exception:
        count = 0
    for index in range(min(count, 8)):
        try:
            name = (await loc.nth(index).inner_text() or "").strip()
            if _is_continue_search(name):
                continue
            if await human_click(loc.nth(index), page=page, timeout=2500):
                return name or "force"
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await pause(page, 0.35, 0.7)
    found = await click_named(page, labels)
    if found and not _is_continue_search(found):
        return found
    aria = await first_visible(
        page.locator('[aria-label*="buscar" i], [aria-label*="pesquisar" i], [data-cy*="search" i]'),
        timeout=1000,
    )
    if aria is not None:
        await aria.click()
        return "aria"
    clicked = await page.evaluate(
        """() => {
          const labels = [...document.querySelectorAll('label')];
          const origem = labels.find((item) => /^origem$/i.test((item.innerText || '').trim()));
          const root = origem
            ? origem.closest('form') || origem.parentElement?.parentElement?.parentElement || document.body
            : document.body;
          const nodes = [...root.querySelectorAll('button, [role=button], a, input[type=submit]')];
          const btn = nodes.find((el) => {
            const label = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`;
            if (/continuar busca/i.test(label)) return false;
            return /buscar passagens|buscar voos|procurar voos|pesquisar voos/i.test(label);
          });
          if (!btn) return null;
          btn.click();
          return (btn.innerText || btn.getAttribute('aria-label') || 'js').slice(0, 40);
        }"""
    )
    if clicked:
        return str(clicked)
    await page.keyboard.press("Enter")
    await pause(page, 0.8, 1.3)
    return "enter"


async def dismiss_banners(page) -> None:
    await click_named(
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
    try:
        menu = page.get_by_role("button", name=re.compile(r"fechar menu", re.I))
        if await menu.count() and await menu.first.is_visible(timeout=600):
            await human_click(menu.first, page=page, timeout=3000)
    except Exception:
        pass


async def type_slowly(page, field, text: str) -> None:
    await human_type(page, field, text)


async def click_field_label(root, labels: tuple[str, ...]) -> None:
    for label in labels:
        raw = label.strip("^$")
        if raw.lower() in {"de", "para", "from", "to", "origin", "destination"}:
            continue
        pattern = re.compile(rf"\b{re.escape(raw)}\b", re.I)
        lab = await first_visible(root.locator("label").filter(has_text=pattern), timeout=400)
        if lab is None:
            lab = await first_visible(root.get_by_text(pattern), timeout=400)
        if lab is not None:
            await human_click(lab, page=host_page(root), timeout=3000)
            return


async def attached_field(locator):
    try:
        if await locator.count():
            return locator.first
    except Exception:
        return None
    return None


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
        attached = await attached_field(extra)
        if attached is not None:
            return attached
    for label in labels:
        pattern = re.compile(label, re.I)
        attached = await attached_field(root.get_by_role("combobox", name=pattern))
        if attached is not None:
            return attached
        attached = await attached_field(root.get_by_label(pattern))
        if attached is not None:
            return attached
    return None


async def airport_selected(field, code: str) -> bool:
    try:
        value = (await field.input_value() or "").strip()
    except Exception:
        value = ""
    if not value:
        return False
    token = code.strip().upper()
    if token not in value.upper():
        return False
    return len(value) > len(token) + 2


async def fill_airport(page, root, labels: tuple[str, ...], code: str, extra=None, require_option: bool = False) -> bool:
    typed = code.strip().upper()
    await click_field_label(root, labels)
    await pause(page, 0.35, 0.7)
    field = await find_field(root, labels, extra=extra)
    if field is None:
        cities = root.get_by_placeholder(re.compile(r"cidade|aeroporto|origem|destino|saindo|indo", re.I))
        field = await first_visible(cities, timeout=800)
    if field is None:
        field = extra.first if extra is not None else None
    if field is None:
        return False
    print(f"Digito {typed} para o select aparecer.", flush=True)
    await human_type(page, field, typed)
    option = None
    for _ in range(8):
        option = await first_visible(page.get_by_role("option", name=re.compile(rf"^{re.escape(typed)}\b", re.I)), timeout=700)
        if option is None:
            option = await first_visible(
                page.get_by_role("option").filter(has_text=re.compile(rf"^{re.escape(typed)}\b", re.I)),
                timeout=400,
            )
        if option is not None:
            break
        await page.wait_for_timeout(400)
    if option is None:
        print(f"O select de {typed} não abriu depois de digitar.", flush=True)
        if require_option:
            return False
        await page.keyboard.press("ArrowDown")
        await pause(page, 0.2, 0.4)
        await page.keyboard.press("Enter")
        return True
    await human_click(option, page=page)
    await pause(page, 0.45, 0.9)
    print(f"Selecionei {typed} no select.", flush=True)
    return True


async def click_calendar_day(page, day: str) -> bool:
    parsed = date.fromisoformat(day)
    day_btn = page.locator(f"#date-{day}, [data-testid='date-{day}'], [data-date='{day}']")
    months = (
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    )
    heading = re.compile(rf"{months[parsed.month - 1]}\s+{parsed.year}", re.I)
    for _ in range(18):
        try:
            if await day_btn.count() and await day_btn.first.is_visible(timeout=500):
                if await day_btn.first.get_attribute("disabled") is None:
                    await human_click(day_btn.first, page=page)
                    await pause(page, 0.35, 0.7)
                    return True
        except Exception:
            pass
        month_box = page.locator("div, section, table").filter(has_text=heading)
        cell = await first_visible(
            month_box.locator(f"[data-date='{day}']").or_(
                month_box.get_by_role("gridcell", name=re.compile(rf"^{parsed.day}$")).or_(
                    month_box.get_by_role("button", name=re.compile(rf"^{parsed.day}$"))
                )
            ),
            timeout=400,
        )
        if cell is not None:
            try:
                if await cell.get_attribute("disabled") is None:
                    await human_click(cell, page=page)
                    await pause(page, 0.35, 0.7)
                    return True
            except Exception:
                await human_click(cell, page=page)
                await pause(page, 0.35, 0.7)
                return True
        nxt = page.locator("[data-datepicker-next='true']")
        next_btn = await first_visible(nxt, timeout=400)
        if next_btn is None:
            next_btn = await first_visible(
                page.get_by_role("button", name=re.compile(r"próximo mês|mes seguinte|next month|ir para o próximo", re.I)),
                timeout=300,
            )
        if next_btn is None:
            break
        try:
            if await next_btn.get_attribute("disabled") is not None:
                break
        except Exception:
            pass
        await human_click(next_btn, page=page)
        await pause(page, 0.3, 0.55)
    return False


async def pick_dates(page, outbound: str, inbound: str | None) -> bool:
    field = await find_field(
        page,
        ("data de ida", "data da ida", "partida", "^ida$", "departure", "datas"),
        extra=page.get_by_placeholder(re.compile(r"dd/mm|aaaa|ida|selecione", re.I)),
    )
    if field is None:
        field = await attached_field(page.locator('[aria-label*="Datas" i], [aria-label*="Ida e volta" i]'))
    await click_field_label(page, ("datas", "ida e volta", "data de ida"))
    if field is not None:
        try:
            await human_click(field, page=page, timeout=2500)
        except Exception:
            pass
        await pause(page, 0.4, 0.8)
    if not await click_calendar_day(page, outbound):
        return False
    if not inbound:
        await close_datepicker(page)
        return True
    ok = await click_calendar_day(page, inbound)
    await close_datepicker(page)
    return ok


async def datepicker_open(page) -> bool:
    confirm = page.get_by_role("button", name=re.compile(r"selecionar datas de ida e volta", re.I))
    if await first_visible(confirm, timeout=250) is not None:
        return True
    if await first_visible(page.locator("[data-datepicker-next='true']"), timeout=200) is not None:
        return True
    return False


async def close_datepicker(page) -> bool:
    btn = page.get_by_role("button", name=re.compile(r"selecionar datas de ida e volta", re.I))
    target = await first_visible(btn, timeout=5000)
    if target is None:
        target = await first_visible(
            page.get_by_text(re.compile(r"^selecionar datas de ida e volta$", re.I)),
            timeout=1500,
        )
    if target is None:
        print("Não achei o botão Selecionar datas de ida e volta.", flush=True)
        return False
    await human_click(target, page=page, timeout=4000)
    print("Cliquei em Selecionar datas de ida e volta para fechar o calendário.", flush=True)
    await pause(page, 0.6, 1.0)
    for _ in range(20):
        if not await datepicker_open(page):
            return True
        await page.wait_for_timeout(200)
    return not await datepicker_open(page)


async def checkbox_checked(box) -> bool:
    try:
        if await box.is_checked():
            return True
    except Exception:
        pass
    try:
        state = (
            await box.get_attribute("aria-checked")
            or await box.get_attribute("aria-pressed")
            or ""
        ).lower()
        return state in {"true", "1"}
    except Exception:
        return False


async def mark_checkbox(page, labels: tuple[str, ...]) -> bool:
    for label in labels:
        pattern = re.compile(label, re.I)
        box = await first_visible(page.get_by_role("checkbox", name=pattern), timeout=900)
        if box is None:
            box = await first_visible(page.get_by_label(pattern), timeout=600)
        if box is None:
            continue
        if await checkbox_checked(box):
            return True
        if not await human_click(box, page=page, timeout=4000):
            return False
        await pause(page, 0.25, 0.45)
        return True
    clicked = await click_named(page, labels, roles=("checkbox", "switch"))
    if clicked:
        return True
    for label in labels:
        text = await first_visible(page.get_by_text(re.compile(label, re.I)), timeout=700)
        if text is not None:
            await human_click(text, page=page, timeout=4000)
            return True
    return False


HOME_POINTS_JS = r"""() => {
  const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const p = [...document.querySelectorAll('p')].find((el) =>
    compact(el.textContent) === 'Usar pontos Azul' && visible(el)
  );
  if (!p) return 'missing';
  const host = p.parentElement;
  if (!host) return 'missing';
  const input = host.querySelector('input[type="checkbox"]');
  const on = () => !!(input && input.checked) || (host.getAttribute('aria-checked') || '').toLowerCase() === 'true';
  if (on()) return 'already';
  if (input) {
    input.click();
  } else {
    const square = [...host.children].find((child) => child !== p && visible(child) && child.getBoundingClientRect().width <= 48);
    (square || host).click();
  }
  return on() ? 'clicked' : 'unchecked';
}"""

POINTS_ON_JS = r"""() => {
  const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const p = [...document.querySelectorAll('p')].find((el) => compact(el.textContent) === 'Usar pontos Azul');
  if (!p) return false;
  const host = p.parentElement;
  const input = host && host.querySelector('input[type="checkbox"]');
  if (input) return !!input.checked;
  const aria = ((host && host.getAttribute('aria-checked')) || '').toLowerCase();
  return aria === 'true' || aria === '1';
}"""


async def azul_points_is_on(page) -> bool:
    try:
        return bool(await page.evaluate(POINTS_ON_JS))
    except Exception:
        return False


async def mark_home_points_checkbox(page, labels: tuple[str, ...]) -> bool:
    del labels
    if await datepicker_open(page):
        return False
    if await azul_points_is_on(page):
        print("O checkbox Usar pontos Azul já estava marcado.", flush=True)
        return True
    pattern = re.compile(r"^usar pontos azul$", re.I)
    chip = await first_visible(page.locator("p").filter(has_text=pattern), timeout=2500)
    if chip is not None:
        host = chip.locator("xpath=..")
        inner = await first_visible(host.locator("input[type='checkbox']"), timeout=400)
        target = inner or host
        await human_click(target, page=page, timeout=4000)
        await pause(page, 0.4, 0.8)
        if await azul_points_is_on(page):
            print("Marquei o checkbox Usar pontos Azul.", flush=True)
            return True
        print("Cliquei em Usar pontos Azul, mas o checkbox não ficou marcado.", flush=True)
        return False
    try:
        result = await page.evaluate(HOME_POINTS_JS)
    except Exception:
        result = "missing"
    if result in {"already", "clicked"} or await azul_points_is_on(page):
        print("Marquei o checkbox Usar pontos Azul.", flush=True)
        return True
    return False


async def wait_and_mark_points(page, labels: tuple[str, ...], tries: int = 12) -> bool:
    for _ in range(tries):
        if await datepicker_open(page):
            await close_datepicker(page)
            await pause(page, 0.4, 0.7)
            continue
        if await mark_home_points_checkbox(page, labels):
            return True
        await page.wait_for_timeout(400)
    return await azul_points_is_on(page)


async def points_toggle_visible(page) -> bool:
    reais = page.get_by_text(re.compile(r"^reais$", re.I))
    pontos = page.get_by_text(re.compile(r"^pontos$", re.I))
    return bool(await first_visible(reais, timeout=400) and await first_visible(pontos, timeout=400))


async def select_points_beside_reais(page) -> bool:
    group = page.locator("div, nav, ul, fieldset, section").filter(
        has=page.get_by_text(re.compile(r"^reais$", re.I))
    ).filter(has=page.get_by_text(re.compile(r"^pontos$", re.I)))
    await first_visible(group, timeout=8000)
    pontos = None
    for role in ("tab", "radio", "button", "switch"):
        pontos = await first_visible(group.get_by_role(role, name=re.compile(r"^pontos$", re.I)), timeout=600)
        if pontos is not None:
            break
    if pontos is None:
        pontos = await first_visible(group.get_by_text(re.compile(r"^pontos$", re.I)), timeout=800)
    if pontos is None:
        pontos = await first_visible(page.get_by_role("tab", name=re.compile(r"^pontos$", re.I)), timeout=800)
    if pontos is None:
        return False
    try:
        state = (await pontos.get_attribute("aria-selected") or await pontos.get_attribute("aria-pressed") or "").lower()
        if state in {"true", "1"}:
            return True
    except Exception:
        pass
    try:
        await human_click(pontos, page=page, timeout=4000)
    except Exception:
        await pontos.click(force=True)
    await pause(page, 0.6, 1.1)
    return True


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


async def click_load_more_flights(page) -> int:
    labels = (
        "ver mais voos",
        "carregar mais voos",
        "mostrar mais voos",
        "carregar mais resultados",
        "^carregar mais$",
        "^ver mais$",
    )
    clicks = 0
    for _ in range(12):
        try:
            for _step in range(random.randint(2, 5)):
                await page.mouse.wheel(0, random.randint(160, 320))
                await pause(page, 0.18, 0.45)
        except Exception:
            pass
        await pause(page, 0.4, 0.7)
        found = await click_named(page, labels, roles=("button", "link"))
        if not found:
            extra = await first_visible(
                page.get_by_text(re.compile(r"ver mais voos|carregar mais voos|mostrar mais voos", re.I)),
                timeout=600,
            )
            if extra is None:
                break
            try:
                await human_click(extra, page=page, timeout=3000)
            except Exception:
                await extra.click(force=True)
            found = "texto"
        clicks += 1
        print(f"Cliquei em mais voos ({found}).", flush=True)
        await pause(page, 1.1, 1.8)
    return clicks


def compact_offer(offer: Offer) -> dict[str, Any]:
    return {
        "origin": offer.origin,
        "destination": offer.destination,
        "departure_date": offer.departure_date,
        "return_date": offer.return_date,
        "departure_time": offer.departure_time,
        "arrival_time": offer.arrival_time,
        "airline": offer.airline,
        "stops": offer.stops,
        "duration": offer.duration,
        "tarifa": offer.fare,
        "trip_kind": offer.trip_kind,
        "milhas": offer.miles,
        "taxas": offer.taxes,
        "program": offer.miles_program,
        "source": offer.source,
        "booking_url": offer.booking_url,
    }


def write_offers_harvest(
    raw_dir: Path,
    job: dict[str, Any],
    status: str,
    url: str,
    offers: list[Offer],
    slim: dict[str, Any],
) -> Path:
    compact = [compact_offer(offer) for offer in offers]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in compact:
        origin = str(item.get("origin") or job["origin"]).upper()
        dest = str(item.get("destination") or job["destination"]).upper()
        day = str(item.get("departure_date") or job["day"])
        groups.setdefault((origin, dest, day), []).append(item)
    last = harvest_route_path(raw_dir, job["origin"], job["destination"], job["day"], job=job)
    if not groups:
        return last
    for (origin, dest, day), items in groups.items():
        file_path = harvest_route_path(raw_dir, origin, dest, day, job=job)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps(
                {
                    "job": {**job, "origin": origin, "destination": dest, "day": day},
                    "status": status,
                    "url": url,
                    "offers": items,
                    "payload": slim,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        kinds = {}
        for item in items:
            fare = item.get("tarifa") or "tarifa"
            kinds[fare] = kinds.get(fare, 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in kinds.items())
        try:
            shown = file_path.relative_to(raw_dir)
        except ValueError:
            shown = Path(file_path.parent.name) / file_path.name
        print(f"Arquivo {shown}: {len(items)} ofertas ({summary}).", flush=True)
        last = file_path
    return last


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
    results_miles_labels: tuple[str, ...] = (),
    require_airport_option: bool = False,
    search_labels: tuple[str, ...],
    blocked_hints: tuple[str, ...],
    parse_offers: Callable[..., list[Offer]],
    booking_url: Callable[..., str],
    miles_after_dates: bool = False,
    read_page: Callable[..., Any] | None = None,
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
        await safe_goto(page, home)
        await dismiss_banners(page)
        await login_with_env(page, program, home, session_mark=session_mark)
        if program == "azul":
            await dismiss_account_overlay(page)

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
            prev = (captured.get("bff_url") or "").lower()
            if program == "azul" and "tudoazul" in prev and "tudoazul" not in url:
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
                if program == "azul":
                    await dismiss_account_overlay(page)
                await pause(page, 1.6, 2.8)
                await wander_mouse(page)
                body_text = ""
                try:
                    body_text = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                if _on_login_page((page.url or "").lower(), body_text):
                    await login_with_env(page, program, home, session_mark=session_mark)
                    try:
                        body_text = (await page.inner_text("body")).lower()
                    except Exception:
                        body_text = ""
                if any(hint in body_text for hint in blocked_hints):
                    status = "throttled"
                    payload = {"error": "throttled", "url": page.url}
                else:
                    if program == "azul":
                        await dismiss_account_overlay(page)
                    if return_day:
                        trip = await click_named(page, round_labels)
                        print(f"Trecho: {trip or 'ida e volta'}.", flush=True)
                    else:
                        trip = await click_named(page, oneway_labels)
                        print(f"Trecho: {trip or 'somente ida'}.", flush=True)
                    await pause(page, 0.4, 0.9)
                    origin_box = page.get_by_role("combobox", name=re.compile(r"^origem$", re.I))
                    dest_box = page.get_by_role("combobox", name=re.compile(r"^destino$", re.I))
                    if not await fill_airport(
                        page, page, origin_labels, origin, extra=origin_box, require_option=require_airport_option
                    ):
                        raise RuntimeError("form_fill_failed:origem")
                    print(f"Origem {origin} selecionada no select.", flush=True)
                    await pause(page, 0.6, 1.1)
                    if not await fill_airport(
                        page, page, dest_labels, dest, extra=dest_box, require_option=require_airport_option
                    ):
                        raise RuntimeError("form_fill_failed:destino")
                    print(f"Destino {dest} selecionado no select.", flush=True)
                    await pause(page, 0.6, 1.1)
                    if miles_labels and not miles_after_dates:
                        marked = await mark_checkbox(page, miles_labels)
                        print("Pontos marcados na home." if marked else "Ainda não achei Pontos na home; sigo.", flush=True)
                    await pause(page, 0.4, 0.9)
                    if not await pick_dates(page, day, return_day):
                        raise RuntimeError("form_fill_failed:data")
                    print("Datas preenchidas.", flush=True)
                    await pause(page, 0.5, 0.9)
                    if miles_labels and miles_after_dates:
                        marked = await wait_and_mark_points(page, miles_labels)
                        if marked and not await azul_points_is_on(page):
                            marked = False
                        print(
                            "Marquei Usar pontos Azul na home, depois das datas."
                            if marked
                            else "O checkbox Usar pontos Azul não ficou marcado. Não busco.",
                            flush=True,
                        )
                        if not marked:
                            raise RuntimeError("form_fill_failed:pontos")
                        await pause(page, 0.35, 0.7)
                    search = await click_home_search(page, search_labels)
                    print(f"Cliquei em buscar passagens ({search}).", flush=True)
                    text = ""
                    if results_miles_labels:
                        for _ in range(wait_loops):
                            if await points_toggle_visible(page):
                                break
                            if await first_visible(
                                page.get_by_text(re.compile(r"ver tarifas|voos encontrados", re.I)),
                                timeout=400,
                            ):
                                break
                            try:
                                text = await page.inner_text("body")
                            except Exception:
                                text = ""
                            if any(hint in (text or "").lower() for hint in blocked_hints):
                                break
                            await page.wait_for_timeout(1000)
                        if not miles_after_dates:
                            captured.pop("bff", None)
                            captured.pop("bff_url", None)
                            captured.pop("bff_status", None)
                        switched = await select_points_beside_reais(page)
                        print("Troquei Reais → Pontos na lista." if switched else "Lista já estava em Pontos, ou o toggle não apareceu.", flush=True)
                    elif miles_after_dates:
                        print("Não clico Pontos na lista; a busca já saiu da home em pontos.", flush=True)
                    for _ in range(wait_loops):
                        try:
                            text = await page.inner_text("body")
                        except Exception:
                            text = ""
                        low = (text or "").lower()
                        if any(hint in low for hint in blocked_hints) or "demorando mais" in low:
                            break
                        if read_page and await first_visible(
                            page.get_by_text(re.compile(r"ver tarifas|voos encontrados", re.I)),
                            timeout=400,
                        ):
                            break
                        if isinstance(captured.get("bff"), dict) and not read_page:
                            trial = parse_offers(captured["bff"], origin, dest, day, return_day)
                            if trial:
                                offers = trial
                                break
                            if not results_miles_labels:
                                break
                        elif not read_page and miles_from_text(text):
                            break
                        await page.wait_for_timeout(1000)
                    if read_page:
                        await browse_results(page)
                        page_offers = await read_page(page, origin, dest, day, return_day)
                        if page_offers:
                            offers = page_offers
                            print(
                                f"Li {len(offers)} tarifas na tela (Pública/Diamante ou Tarifa), sem clicar em voo.",
                                flush=True,
                            )
                    payload["url"] = page.url
                    payload["network"] = captured.get("urls") or []
                    if captured.get("bff_url"):
                        payload["bff_url"] = captured.get("bff_url")
                        payload["bff_status"] = captured.get("bff_status")
                    blocked = "403" in " ".join(captured.get("urls") or []) or any(
                        hint in (text or "").lower() for hint in blocked_hints
                    )
                    if not offers and isinstance(captured.get("bff"), dict):
                        offers = parse_offers(captured["bff"], origin, dest, day, return_day)
                    if offers:
                        pass
                    elif blocked:
                        status = "throttled"
                        payload["error"] = "throttled"
                    elif not read_page:
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
                    else:
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
                    print(f"Azul/coleta: {message.splitlines()[0][:180]}", flush=True)
            compact = [compact_offer(offer) for offer in offers]
            url = booking_url(origin, dest, day, return_day)
            if save_raw:
                slim = {k: v for k, v in payload.items() if k not in {"bff", "dom_text"}}
                if compact:
                    file_path = write_offers_harvest(raw_dir, job, status, url, offers, slim)
                else:
                    file_path = harvest_route_path(raw_dir, origin, dest, day, job=job)
                    keep_old = False
                    if file_path.exists():
                        try:
                            previous = json.loads(file_path.read_text(encoding="utf-8"))
                            keep_old = bool(previous.get("offers"))
                        except (OSError, json.JSONDecodeError):
                            keep_old = False
                    azul = str(job.get("program") or raw_dir.name).lower() == "azul"
                    if not keep_old and not azul:
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
                if not await rest_on_results_then_home(page, home, wait):
                    break
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
        print(
            f"Abro a {label} e entro com o login do .env, se ainda não estiver na conta.",
            flush=True,
        )
        if await login_with_env(page, _program_from_label(label), home, session_mark=session_mark):
            href = page.url
            await page.wait_for_timeout(4000)
            await context.close()
            return {"ok": True, "url": href}
        deadline = asyncio.get_event_loop().time() + 25 * 60
        href = page.url
        ok = False
        last_nudge = 0.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                href = page.url
                body = ""
                try:
                    body = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                logged_out = any(
                    token in (href or "").lower()
                    for token in ("/login", "/signin", "auth.", "sso.", "identidade")
                )
                if not logged_out and any(hint in body for hint in logged_hints):
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    session_mark.write_text("ok\n", encoding="utf-8")
                    ok = True
                    print(f"Sessão {label} reconhecida. Deixo a janela aberta mais uns segundos.", flush=True)
                    await page.wait_for_timeout(12000)
                    break
                now = asyncio.get_event_loop().time()
                if now - last_nudge >= 60:
                    print(f"Ainda espero o login da {label} nesta janela…", flush=True)
                    last_nudge = now
                await page.wait_for_timeout(2000)
            except Exception as exc:
                if browser_closed(exc):
                    return {"ok": ok, "url": href, "error": "browser_closed"}
                await page.wait_for_timeout(2000)
        await context.close()
        return {"ok": ok, "url": href}
