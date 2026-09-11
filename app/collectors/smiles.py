from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.collectors.browser import collect_home_jobs, open_login
from app.config import SMILES_PROFILE_DIR
from app.providers.base import Offer
from app.providers.brazilian import _parse_smiles, miles_values_from_json, smiles_url

SESSION_MARK = SMILES_PROFILE_DIR / ".logged_in"
SMILES_HOME = "https://www.smiles.com.br/mfe/emissao-passagem"
BFF_HINTS = (
    "air-flightsearch",
    "airlines/search",
    "flightsearch",
    "emissao",
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
LOGGED = ("olá,", "ola,", "encerrar sessão", "sair", "meus dados")


def smiles_session_ready() -> bool:
    return SESSION_MARK.exists() and SMILES_PROFILE_DIR.exists()


def parse_smiles_payload(payload: dict[str, Any], origin: str, dest: str, day: str, return_day: str | None) -> list[Offer]:
    if payload.get("source") == "dom" and payload.get("miles"):
        return [
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
            )
        ]
    for candidate in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
        if not candidate:
            continue
        offers = _parse_smiles(candidate, origin, dest, day, return_day)
        if offers:
            return offers
    values = sorted(set(miles_values_from_json(payload)))
    return [
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
        )
        for amount in values
    ]


async def collect_smiles_jobs(jobs: list[dict[str, Any]], raw_dir: Path, **kwargs) -> list[dict[str, Any]]:
    kwargs.pop("redemption", None)
    if "pause_seconds" not in kwargs and "pause" in kwargs:
        kwargs["pause_seconds"] = kwargs.pop("pause")
    else:
        kwargs.pop("pause", None)
    return await collect_home_jobs(
        jobs,
        raw_dir,
        profile_dir=SMILES_PROFILE_DIR,
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
        session_mark=SESSION_MARK,
        **kwargs,
    )


async def open_smiles_login() -> dict[str, Any]:
    return await open_login(SMILES_PROFILE_DIR, SMILES_HOME, "GOL Smiles", LOGGED, SESSION_MARK)


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "login"
    if command != "login":
        raise SystemExit("Use: python3 -m app.collectors.smiles login")
    result = asyncio.run(open_smiles_login())
    print("Sessão Smiles salva." if result.get("ok") else "Ainda precisa fazer login.", flush=True)
    print(result.get("url") or "", flush=True)
