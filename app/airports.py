from __future__ import annotations

import re
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
    "GYN": {"city": "Goiânia", "country": "Brasil", "national": True},
    "CGR": {"city": "Campo Grande", "country": "Brasil", "national": True},
    "PVH": {"city": "Porto Velho", "country": "Brasil", "national": True},
    "RBR": {"city": "Rio Branco", "country": "Brasil", "national": True},
    "BVB": {"city": "Boa Vista", "country": "Brasil", "national": True},
    "MCP": {"city": "Macapá", "country": "Brasil", "national": True},
    "STM": {"city": "Santarém", "country": "Brasil", "national": True},
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
    "FLL": {"city": "Fort Lauderdale", "country": "EUA", "national": False},
    "MCO": {"city": "Orlando", "country": "EUA", "national": False},
    "JFK": {"city": "Nova York", "country": "EUA", "national": False},
    "LAX": {"city": "Los Angeles", "country": "EUA", "national": False},
    "EZE": {"city": "Buenos Aires", "country": "Argentina", "national": False},
    "COR": {"city": "Córdoba", "country": "Argentina", "national": False},
    "ASU": {"city": "Assunção", "country": "Paraguai", "national": False},
    "MVD": {"city": "Montevidéu", "country": "Uruguai", "national": False},
    "CUZ": {"city": "Cusco", "country": "Peru", "national": False},
    "CTG": {"city": "Cartagena", "country": "Colômbia", "national": False},
    "CUN": {"city": "Cancún", "country": "México", "national": False},
    "PUJ": {"city": "Punta Cana", "country": "República Dominicana", "national": False},
    "BRU": {"city": "Bruxelas", "country": "Bélgica", "national": False},
    "DXB": {"city": "Dubai", "country": "Emirados Árabes", "national": False},
    "DOH": {"city": "Doha", "country": "Catar", "national": False},
    "JNB": {"city": "Joanesburgo", "country": "África do Sul", "national": False},
    "CPT": {"city": "Cidade do Cabo", "country": "África do Sul", "national": False},
    "SCL": {"city": "Santiago", "country": "Chile", "national": False},
    "LIM": {"city": "Lima", "country": "Peru", "national": False},
    "BOG": {"city": "Bogotá", "country": "Colômbia", "national": False},
    "MEX": {"city": "Cidade do México", "country": "México", "national": False},
    "FRA": {"city": "Frankfurt", "country": "Alemanha", "national": False},
    "BER": {"city": "Berlim", "country": "Alemanha", "national": False},
    "MUC": {"city": "Munique", "country": "Alemanha", "national": False},
    "BRC": {"city": "Bariloche", "country": "Argentina", "national": False},
    "MDZ": {"city": "Mendoza", "country": "Argentina", "national": False},
    "PDP": {"city": "Punta del Este", "country": "Uruguai", "national": False},
    "CUR": {"city": "Curaçao", "country": "Curaçao", "national": False},
    "NRT": {"city": "Tóquio", "country": "Japão", "national": False},
    "PEK": {"city": "Pequim", "country": "China", "national": False},
    "PVG": {"city": "Xangai", "country": "China", "national": False},
}

FEATURED_NATIONAL = ["SSA", "FOR", "REC", "FLN", "BSB", "POA", "CWB", "BPS", "NAT", "IGU", "MCZ"]
FEATURED_INTERNATIONAL = ["LIS", "MAD", "MIA", "EZE", "SCL", "BCN", "FCO", "CDG", "DXB", "CUN"]

CITY_AIRPORTS = {
    "RIO": ("GIG", "SDU"),
}
CITY_CODE_PROGRAMS = {"latam", "azul", "smiles"}
RIO_CODES = ("GIG", "SDU", "RIO")
ORIGIN_LABELS = {
    "GIG": "Galeão",
    "SDU": "Santos Dumont",
    "VIX": "Vitória",
    "GRU": "Guarulhos",
    "RIO": "Rio de Janeiro",
}


def expand_city_airports(code: str) -> list[str]:
    token = (code or "").strip().upper()
    if token in {"RJ", "RIO DE JANEIRO"}:
        token = "RIO"
    return list(CITY_AIRPORTS.get(token, (token,)))


def related_airports(code: str | None) -> list[str]:
    """Busca é RIO; no site GIG e SDU ficam separados. RIO antigo ainda aparece nos dois."""
    token = (code or "").strip().upper()
    if token in {"RJ", "RIO DE JANEIRO"}:
        token = "RIO"
    if token == "GIG":
        return ["GIG", "RIO"]
    if token == "SDU":
        return ["SDU", "RIO"]
    if token == "RIO":
        return ["GIG", "SDU", "RIO"]
    return [token] if token else []


_AIRPORT_HINTS = (
    ("santos dumont", "SDU"),
    ("antonio carlos jobim", "GIG"),
    ("galeao", "GIG"),
    ("galeão", "GIG"),
    ("guarulhos", "GRU"),
    ("congonhas", "CGH"),
    ("viracopos", "VCP"),
    ("galeão", "GIG"),
)


def iata_from_card(text: str | None, fallback: str) -> str:
    """Lê GIG/SDU (e outros IATA) do texto do card da LATAM."""
    blob = text or ""
    upper = blob.upper()
    rio_context = (fallback or "").strip().upper() in {"RIO", "GIG", "SDU", "RJ", "RIO DE JANEIRO"}
    if rio_context:
        if re.search(r"\bGIG\b", upper):
            return "GIG"
        if re.search(r"\bSDU\b", upper):
            return "SDU"
    for code in AIRPORTS:
        if code == "RIO":
            continue
        if re.search(rf"\b{code}\b", upper):
            return code
    folded = blob.lower().replace("ã", "a").replace("á", "a").replace("é", "e")
    for hint, code in _AIRPORT_HINTS:
        if hint.replace("ã", "a") in folded:
            return code
    token = (fallback or "").strip().upper()
    return token


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


def resolve_airport_query(raw: str | None) -> str:
    token = (raw or "").strip()
    if not token:
        return ""
    upper = token.upper()
    if upper in AIRPORTS:
        return upper
    folded = token.casefold()
    for code, info in AIRPORTS.items():
        if str(info["city"]).casefold() == folded:
            return code
    match = re.search(r"\b([A-Z]{3})\b", upper)
    return match.group(1) if match else upper


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
    origin = (row.get("origin") or "").upper()
    info = meta(dest)
    origin_info = meta(origin)
    return {
        **row,
        "origin": origin,
        "destination": dest,
        "city": info["city"],
        "origin_city": origin_info["city"],
        "country": info["country"],
        "national": bool(info["national"]),
        "image": image_url(dest),
    }
