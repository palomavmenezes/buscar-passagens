from __future__ import annotations

from urllib.parse import quote_plus

AIRPORTS: dict[str, dict[str, str | bool]] = {
    "GIG": {"city": "Rio de Janeiro", "country": "Brasil", "national": True},
    "SDU": {"city": "Rio de Janeiro", "country": "Brasil", "national": True},
    "RIO": {"city": "Rio de Janeiro", "country": "Brasil", "national": True},
    "GRU": {"city": "São Paulo", "country": "Brasil", "national": True},
    "CGH": {"city": "São Paulo", "country": "Brasil", "national": True},
    "VCP": {"city": "Campinas", "country": "Brasil", "national": True},
    "VIX": {"city": "Vitória", "country": "Brasil", "national": True},
    "BSB": {"city": "Brasília", "country": "Brasil", "national": True},
    "CNF": {"city": "Belo Horizonte", "country": "Brasil", "national": True},
    "SSA": {"city": "Salvador", "country": "Brasil", "national": True},
    "FOR": {"city": "Fortaleza", "country": "Brasil", "national": True},
    "REC": {"city": "Recife", "country": "Brasil", "national": True},
    "NAT": {"city": "Natal", "country": "Brasil", "national": True},
    "MCZ": {"city": "Maceió", "country": "Brasil", "national": True},
    "JPA": {"city": "João Pessoa", "country": "Brasil", "national": True},
    "AJU": {"city": "Aracaju", "country": "Brasil", "national": True},
    "SLZ": {"city": "São Luís", "country": "Brasil", "national": True},
    "THE": {"city": "Teresina", "country": "Brasil", "national": True},
    "BEL": {"city": "Belém", "country": "Brasil", "national": True},
    "MAO": {"city": "Manaus", "country": "Brasil", "national": True},
    "CGB": {"city": "Cuiabá", "country": "Brasil", "national": True},
    "CWB": {"city": "Curitiba", "country": "Brasil", "national": True},
    "POA": {"city": "Porto Alegre", "country": "Brasil", "national": True},
    "FLN": {"city": "Florianópolis", "country": "Brasil", "national": True},
    "IGU": {"city": "Foz do Iguaçu", "country": "Brasil", "national": True},
    "BPS": {"city": "Porto Seguro", "country": "Brasil", "national": True},
    "NVT": {"city": "Navegantes", "country": "Brasil", "national": True},
    "LIS": {"city": "Lisboa", "country": "Portugal", "national": False},
    "OPO": {"city": "Porto", "country": "Portugal", "national": False},
    "MAD": {"city": "Madrid", "country": "Espanha", "national": False},
    "BCN": {"city": "Barcelona", "country": "Espanha", "national": False},
    "FCO": {"city": "Roma", "country": "Itália", "national": False},
    "MXP": {"city": "Milão", "country": "Itália", "national": False},
    "CDG": {"city": "Paris", "country": "França", "national": False},
    "LHR": {"city": "Londres", "country": "Reino Unido", "national": False},
    "AMS": {"city": "Amsterdã", "country": "Holanda", "national": False},
    "MIA": {"city": "Miami", "country": "EUA", "national": False},
    "MCO": {"city": "Orlando", "country": "EUA", "national": False},
    "JFK": {"city": "Nova York", "country": "EUA", "national": False},
    "EZE": {"city": "Buenos Aires", "country": "Argentina", "national": False},
    "SCL": {"city": "Santiago", "country": "Chile", "national": False},
    "LIM": {"city": "Lima", "country": "Peru", "national": False},
    "BOG": {"city": "Bogotá", "country": "Colômbia", "national": False},
    "MEX": {"city": "Cidade do México", "country": "México", "national": False},
    "FRA": {"city": "Frankfurt", "country": "Alemanha", "national": False},
    "BER": {"city": "Berlim", "country": "Alemanha", "national": False},
    "MUC": {"city": "Munique", "country": "Alemanha", "national": False},
    "BRC": {"city": "Bariloche", "country": "Argentina", "national": False},
    "NRT": {"city": "Tóquio", "country": "Japão", "national": False},
    "PEK": {"city": "Pequim", "country": "China", "national": False},
    "PVG": {"city": "Xangai", "country": "China", "national": False},
}

FEATURED_NATIONAL = ["SSA", "FOR", "REC", "FLN", "BSB", "POA", "CWB", "BPS", "NAT", "IGU", "MCZ"]
FEATURED_INTERNATIONAL = ["LIS", "MAD", "MIA", "EZE", "SCL", "BCN", "FCO", "CDG", "MCO", "OPO"]

CITY_AIRPORTS = {
    "RIO": ("GIG", "SDU"),
}
CITY_CODE_PROGRAMS = {"latam", "azul", "smiles"}


def expand_city_airports(code: str) -> list[str]:
    token = (code or "").strip().upper()
    if token in {"RJ", "RIO DE JANEIRO"}:
        token = "RIO"
    return list(CITY_AIRPORTS.get(token, (token,)))


def collapse_rio_codes(codes: list[str], program: str | None = None) -> list[str]:
    cleaned = [item.strip().upper() for item in codes if item and str(item).strip()]
    if program and program not in CITY_CODE_PROGRAMS:
        return cleaned
    if "GIG" not in cleaned or "SDU" not in cleaned:
        return cleaned
    out: list[str] = []
    seen: set[str] = set()
    for code in cleaned:
        mapped = "RIO" if code in {"GIG", "SDU", "RIO"} else code
        if mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out


def meta(code: str) -> dict[str, str | bool]:
    info = AIRPORTS.get(code.upper())
    if info:
        return info
    return {"city": code.upper(), "country": "", "national": False}


def is_national(code: str) -> bool:
    return bool(meta(code).get("national"))


def city_name(code: str) -> str:
    return str(meta(code)["city"])


def image_url(code: str) -> str:
    info = meta(code)
    prompt = f"cinematic travel photograph of {info['city']}, {info['country']}, famous landmark, golden hour"
    return (
        "https://image.pollinations.ai/prompt/"
        + quote_plus(prompt)
        + "?width=960&height=620&nologo=true"
    )


def decorate(row: dict) -> dict:
    dest = (row.get("destination") or "").upper()
    info = meta(dest)
    return {
        **row,
        "destination": dest,
        "city": info["city"],
        "country": info["country"],
        "national": bool(info["national"]),
        "image": image_url(dest),
    }
