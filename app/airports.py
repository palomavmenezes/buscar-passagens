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
    "SAO": {"city": "São Paulo (CGH, GRU, VCP)", "country": "Brasil", "national": True},
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
    "RAO": {"city": "Ribeirão Preto", "country": "Brasil", "national": True},
    "SJK": {"city": "São José dos Campos", "country": "Brasil", "national": True},
    "UDI": {"city": "Uberlândia", "country": "Brasil", "national": True},
    "IPN": {"city": "Ipatinga", "country": "Brasil", "national": True},
    "IOS": {"city": "Ilhéus", "country": "Brasil", "national": True},
    "VDC": {"city": "Vitória da Conquista", "country": "Brasil", "national": True},
    "PNZ": {"city": "Petrolina", "country": "Brasil", "national": True},
    "FEN": {"city": "Fernando de Noronha", "country": "Brasil", "national": True},
    "JJD": {"city": "Jericoacoara", "country": "Brasil", "national": True},
    "JDO": {"city": "Juazeiro do Norte", "country": "Brasil", "national": True},
    "CPV": {"city": "Campina Grande", "country": "Brasil", "national": True},
    "IMP": {"city": "Imperatriz", "country": "Brasil", "national": True},
    "PHB": {"city": "Parnaíba", "country": "Brasil", "national": True},
    "LDB": {"city": "Londrina", "country": "Brasil", "national": True},
    "MGF": {"city": "Maringá", "country": "Brasil", "national": True},
    "JOI": {"city": "Joinville", "country": "Brasil", "national": True},
    "XAP": {"city": "Chapecó", "country": "Brasil", "national": True},
    "CXJ": {"city": "Caxias do Sul", "country": "Brasil", "national": True},
    "PET": {"city": "Pelotas", "country": "Brasil", "national": True},
    "RIA": {"city": "Santa Maria", "country": "Brasil", "national": True},
    "MAB": {"city": "Marabá", "country": "Brasil", "national": True},
    "PMW": {"city": "Palmas", "country": "Brasil", "national": True},
    "OPS": {"city": "Sinop", "country": "Brasil", "national": True},
    "BYO": {"city": "Bonito", "country": "Brasil", "national": True},
    "AEP": {"city": "Buenos Aires", "country": "Argentina", "national": False},
    "USH": {"city": "Ushuaia", "country": "Argentina", "national": False},
    "DFW": {"city": "Dallas", "country": "EUA", "national": False},
    "ORD": {"city": "Chicago", "country": "EUA", "national": False},
    "ORY": {"city": "Paris", "country": "França", "national": False},
    "IST": {"city": "Istambul", "country": "Turquia", "national": False},
    "YYZ": {"city": "Toronto", "country": "Canadá", "national": False},
    "YVR": {"city": "Vancouver", "country": "Canadá", "national": False},
    "MDE": {"city": "Medellín", "country": "Colômbia", "national": False},
    "VVI": {"city": "Santa Cruz de la Sierra", "country": "Bolívia", "national": False},
    "PTY": {"city": "Cidade do Panamá", "country": "Panamá", "national": False},
}

BRAZIL_STATES: dict[str, tuple[str, str]] = {
    "GIG": ("RJ", "Rio de Janeiro"),
    "SDU": ("RJ", "Rio de Janeiro"),
    "RIO": ("RJ", "Rio de Janeiro"),
    "GRU": ("SP", "São Paulo"),
    "CGH": ("SP", "São Paulo"),
    "VCP": ("SP", "São Paulo"),
    "SAO": ("SP", "São Paulo"),
    "VIX": ("ES", "Espírito Santo"),
    "BSB": ("DF", "Distrito Federal"),
    "CNF": ("MG", "Minas Gerais"),
    "SSA": ("BA", "Bahia"),
    "BPS": ("BA", "Bahia"),
    "FOR": ("CE", "Ceará"),
    "REC": ("PE", "Pernambuco"),
    "NAT": ("RN", "Rio Grande do Norte"),
    "MCZ": ("AL", "Alagoas"),
    "JPA": ("PB", "Paraíba"),
    "AJU": ("SE", "Sergipe"),
    "SLZ": ("MA", "Maranhão"),
    "THE": ("PI", "Piauí"),
    "BEL": ("PA", "Pará"),
    "STM": ("PA", "Pará"),
    "MAO": ("AM", "Amazonas"),
    "CGB": ("MT", "Mato Grosso"),
    "GYN": ("GO", "Goiás"),
    "CGR": ("MS", "Mato Grosso do Sul"),
    "PVH": ("RO", "Rondônia"),
    "RBR": ("AC", "Acre"),
    "BVB": ("RR", "Roraima"),
    "MCP": ("AP", "Amapá"),
    "CWB": ("PR", "Paraná"),
    "IGU": ("PR", "Paraná"),
    "POA": ("RS", "Rio Grande do Sul"),
    "FLN": ("SC", "Santa Catarina"),
    "NVT": ("SC", "Santa Catarina"),
    "RAO": ("SP", "São Paulo"),
    "SJK": ("SP", "São Paulo"),
    "UDI": ("MG", "Minas Gerais"),
    "IPN": ("MG", "Minas Gerais"),
    "IOS": ("BA", "Bahia"),
    "VDC": ("BA", "Bahia"),
    "PNZ": ("PE", "Pernambuco"),
    "FEN": ("PE", "Pernambuco"),
    "JJD": ("CE", "Ceará"),
    "JDO": ("CE", "Ceará"),
    "CPV": ("PB", "Paraíba"),
    "IMP": ("MA", "Maranhão"),
    "PHB": ("PI", "Piauí"),
    "LDB": ("PR", "Paraná"),
    "MGF": ("PR", "Paraná"),
    "JOI": ("SC", "Santa Catarina"),
    "XAP": ("SC", "Santa Catarina"),
    "CXJ": ("RS", "Rio Grande do Sul"),
    "PET": ("RS", "Rio Grande do Sul"),
    "RIA": ("RS", "Rio Grande do Sul"),
    "MAB": ("PA", "Pará"),
    "PMW": ("TO", "Tocantins"),
    "OPS": ("MT", "Mato Grosso"),
    "BYO": ("MS", "Mato Grosso do Sul"),
}

FEATURED_NATIONAL = ["SSA", "FOR", "REC", "FLN", "BSB", "POA", "CWB", "BPS", "NAT", "IGU", "MCZ"]
FEATURED_INTERNATIONAL = ["LIS", "MAD", "MIA", "EZE", "SCL", "BCN", "FCO", "CDG", "DXB", "CUN"]

CITY_AIRPORTS = {
    "RIO": ("GIG", "SDU"),
    "SAO": ("CGH", "GRU", "VCP"),
}
CITY_CODE_PROGRAMS = {"latam", "azul", "smiles"}
CITY_MEMBER_CODES = tuple(code for members in CITY_AIRPORTS.values() for code in members)
RIO_CODES = ("GIG", "SDU", "RIO")
SAO_CODES = ("CGH", "GRU", "VCP", "SAO")
ORIGIN_LABELS = {
    "GIG": "Galeão",
    "SDU": "Santos Dumont",
    "VIX": "Vitória",
    "GRU": "Guarulhos",
    "RIO": "Rio de Janeiro",
    "SAO": "São Paulo",
}
CITY_ALIASES = {
    "RJ": "RIO",
    "RIO DE JANEIRO": "RIO",
    "SAO PAULO": "SAO",
    "SÃO PAULO": "SAO",
    "SAOPAULO": "SAO",
    "SAMPA": "SAO",
}


def normalize_city_token(code: str | None) -> str:
    token = (code or "").strip().upper()
    if not token:
        return ""
    folded = (
        token.replace("Á", "A")
        .replace("Â", "A")
        .replace("Ã", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ô", "O")
        .replace("Ú", "U")
        .replace("Ç", "C")
    )
    compact = folded.replace(" ", "")
    return CITY_ALIASES.get(token) or CITY_ALIASES.get(folded) or CITY_ALIASES.get(compact) or token


def city_group(code: str | None) -> set[str]:
    token = normalize_city_token(code)
    for city, members in CITY_AIRPORTS.items():
        group = {city, *members}
        if token in group:
            return group
    return {token} if token else set()


def same_city(left: str | None, right: str | None) -> bool:
    a, b = normalize_city_token(left), normalize_city_token(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return bool(city_group(a) & city_group(b)) and a in city_group(b)


def expand_city_airports(code: str) -> list[str]:
    token = normalize_city_token(code)
    return list(CITY_AIRPORTS.get(token, (token,)))


def related_airports(code: str | None) -> list[str]:
    """Busca é RIO/SAO; no banco o aeroporto real (GIG, CGH…) fica separado."""
    token = normalize_city_token(code)
    if not token:
        return []
    for city, members in CITY_AIRPORTS.items():
        if token == city:
            return [city, *members]
        if token in members:
            return [token, city]
    return [token]


def city_search_note(origin: str, dest: str) -> str:
    bits = []
    for code in (origin, dest):
        token = normalize_city_token(code)
        members = CITY_AIRPORTS.get(token)
        if members:
            bits.append(f"{token} cobre {'/'.join(members)}")
    return f" ({'; '.join(bits)})" if bits else ""


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
    """Lê o aeroporto real (GIG/SDU/CGH/GRU/VCP) do texto do card."""
    blob = text or ""
    upper = blob.upper()
    fallback_token = normalize_city_token(fallback)
    members = CITY_AIRPORTS.get(fallback_token)
    if members is None:
        for city, codes in CITY_AIRPORTS.items():
            if fallback_token in codes:
                members = codes
                break
    if members:
        for code in members:
            if re.search(rf"\b{code}\b", upper):
                return code
    for code in AIRPORTS:
        if code in CITY_AIRPORTS:
            continue
        if re.search(rf"\b{code}\b", upper):
            return code
    folded = blob.lower().replace("ã", "a").replace("á", "a").replace("é", "e")
    for hint, code in _AIRPORT_HINTS:
        if hint.replace("ã", "a") in folded:
            return code
    token = (fallback or "").strip().upper()
    return token


def collapse_city_codes(codes: list[str], program: str | None = None) -> list[str]:
    cleaned = [normalize_city_token(item) for item in codes if item and str(item).strip()]
    cleaned = [item for item in cleaned if item]
    if program and program not in CITY_CODE_PROGRAMS:
        return cleaned
    present = set(cleaned)
    collapse_to: set[str] = set()
    for city, members in CITY_AIRPORTS.items():
        hits = sum(1 for code in members if code in present)
        if hits >= 2 or (city in present and hits):
            collapse_to.add(city)
    out: list[str] = []
    seen: set[str] = set()
    for code in cleaned:
        mapped = code
        for city, members in CITY_AIRPORTS.items():
            if city in collapse_to and code in {city, *members}:
                mapped = city
                break
        if mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out


def collapse_rio_codes(codes: list[str], program: str | None = None) -> list[str]:
    return collapse_city_codes(codes, program)


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
    city = normalize_city_token(token)
    if city in CITY_AIRPORTS or city in AIRPORTS:
        return city
    upper = token.upper()
    if upper in AIRPORTS:
        return upper
    folded = token.casefold()
    sao_names = {"são paulo", "sao paulo", "sampa"}
    if folded in sao_names:
        return "SAO"
    rio_names = {"rio de janeiro", "rio"}
    if folded in rio_names:
        return "RIO"
    for code, info in AIRPORTS.items():
        if code in CITY_AIRPORTS:
            continue
        if str(info["city"]).casefold() == folded:
            return code
    match = re.search(r"\b([A-Z]{3})\b", upper)
    return match.group(1) if match else upper


def fallback_image_url(code: str) -> str:
    info = meta(code)
    prompt = f"cinematic travel photograph of {info['city']}, {info['country']}, famous landmark, golden hour"
    return (
        "https://image.pollinations.ai/prompt/"
        + quote_plus(prompt)
        + "?width=960&height=620&nologo=true"
    )


def image_url(code: str) -> str:
    from app.media import destination_image_url

    return destination_image_url(code) or fallback_image_url(code)


def decorate(row: dict) -> dict:
    from app.media import destination_image_url, program_logo_url

    dest = (row.get("destination") or "").upper()
    origin = (row.get("origin") or "").upper()
    info = meta(dest)
    origin_info = meta(origin)
    local = destination_image_url(dest, str(info["city"]), bool(info["national"]))
    return {
        **row,
        "origin": origin,
        "destination": dest,
        "city": info["city"],
        "origin_city": origin_info["city"],
        "country": info["country"],
        "national": bool(info["national"]),
        "image": local or fallback_image_url(dest),
        "image_local": bool(local),
        "logo": program_logo_url(row.get("miles_program"), row.get("airline")),
    }
