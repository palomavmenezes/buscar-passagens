from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.collectors.browser import collect_home_jobs, open_login
from app.config import AZUL_PROFILE_DIR
from app.providers.base import Offer
from app.providers.brazilian import _parse_azul, azul_url, miles_values_from_json

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
LOGGED = ("olá,", "ola,", "encerrar sessão", "sair da conta", "minha conta")


def azul_session_ready() -> bool:
    return SESSION_MARK.exists() and AZUL_PROFILE_DIR.exists()


def parse_azul_payload(payload: dict[str, Any], origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    if payload.get("source") == "dom" and payload.get("miles"):
        return [
            Offer(
                origin=origin,
                destination=dest,
                departure_date=day,
                return_date=return_day,
                airline="Azul",
                stops=None,
                cabin="economy",
                price_type="miles",
                currency="BRL",
                price_cash=None,
                miles=int(payload["miles"]),
                miles_program="azul",
                taxes=None,
                source="tudoazul",
                booking_url=azul_url(origin, dest, day, return_day),
            )
        ]
    offers = _parse_azul(payload, origin, dest, day, return_day)
    if offers:
        return offers
    wrapped = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if wrapped:
        offers = _parse_azul(wrapped, origin, dest, day, return_day)
        if offers:
            return offers
    values = sorted(set(miles_values_from_json(payload)))
    return [
        Offer(
            origin=origin,
            destination=dest,
            departure_date=day,
            return_date=return_day,
            airline="Azul",
            stops=None,
            cabin="economy",
            price_type="miles",
            currency="BRL",
            price_cash=None,
            miles=amount,
            miles_program="azul",
            taxes=None,
            source="tudoazul",
            booking_url=azul_url(origin, dest, day, return_day),
        )
        for amount in values
    ]


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
        round_labels=("ida e volta", "round trip"),
        oneway_labels=("trecho único", "somente ida", "só ida", "one way"),
        origin_labels=("origem", "^de$", "origin", "from"),
        dest_labels=("destino", "^para$", "destination", "to"),
        miles_labels=("usar pontos", "pontos tudoazul", "tudoazul", "pagar com pontos", "usar milhas"),
        search_labels=("buscar voos", "procurar voos", "buscar", "pesquisar"),
        blocked_hints=BLOCKED,
        parse_offers=parse_azul_payload,
        booking_url=lambda origin, dest, day, back: azul_url(origin, dest, day, back),
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
