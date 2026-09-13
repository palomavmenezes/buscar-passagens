from __future__ import annotations

import unicodedata
from pathlib import Path

from app.config import ROOT

IMG_ROOT = ROOT / "app" / "static" / "imgs"
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg", ".gif"}
LOGO_ALIASES = {
    "latam": ("latam", "latampass", "latam-pass"),
    "azul": ("azul", "tudoazul"),
    "smiles": ("smiles", "gol"),
}
_CACHE: dict[str, object] = {"stamp": None, "dest": {}, "logos": {}}


def fold_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    cleaned = "".join(char if char.isalnum() else "-" for char in plain)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def _static_url(path: Path) -> str:
    rel = path.resolve().relative_to((ROOT / "app" / "static").resolve()).as_posix()
    return f"/static/{rel}"


def _kind_of(path: Path) -> str:
    parent = fold_stem(path.parent.name)
    if "logo" in parent:
        return "logo"
    if parent.startswith("inter"):
        return "international"
    if parent.startswith("nacio"):
        return "national"
    return ""


def _stems_for_file(path: Path) -> list[str]:
    stem = fold_stem(path.stem)
    if not stem:
        return []
    keys = [stem]
    parts = [part for part in stem.split("-") if part]
    if parts and len(parts[-1]) == 3:
        keys.append(parts[-1])
    return list(dict.fromkeys(keys))


def _dir_stamp() -> tuple:
    if not IMG_ROOT.exists():
        return ()
    stamps = []
    for folder in IMG_ROOT.iterdir():
        if folder.is_dir():
            stamps.append((folder.name.lower(), folder.stat().st_mtime))
    return tuple(sorted(stamps))


def _load_indexes() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    stamp = _dir_stamp()
    if _CACHE["stamp"] == stamp:
        return _CACHE["dest"], _CACHE["logos"]  # type: ignore[return-value]
    dest: dict[str, dict[str, str]] = {"national": {}, "international": {}}
    logos: dict[str, str] = {}
    if IMG_ROOT.exists():
        for path in IMG_ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in EXTS:
                continue
            kind = _kind_of(path)
            url = _static_url(path)
            if kind == "logo":
                for key in _stems_for_file(path):
                    logos.setdefault(key, url)
                continue
            bucket = dest.get(kind)
            if bucket is None:
                continue
            for key in _stems_for_file(path):
                bucket.setdefault(key, url)
    _CACHE["stamp"] = stamp
    _CACHE["dest"] = dest
    _CACHE["logos"] = logos
    return dest, logos


def _city_slug(city: str) -> str:
    return fold_stem((city or "").split("(")[0])


def destination_keys(code: str, city: str = "", national: bool | None = None) -> list[str]:
    from app.airports import CITY_AIRPORTS, meta

    token = (code or "").upper()
    info = meta(token)
    iata = fold_stem(token)
    city_slug = _city_slug(city or str(info.get("city") or token))
    keys = [f"{city_slug}-{iata}", iata, city_slug]
    if token in CITY_AIRPORTS:
        keys.append(fold_stem(token))
    else:
        for city_code, members in CITY_AIRPORTS.items():
            if token in members:
                group_slug = _city_slug(str(meta(city_code).get("city") or city_code))
                keys.insert(0, f"{group_slug}-{iata}")
                keys.append(group_slug)
                break
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def destination_image_url(code: str, city: str = "", national: bool | None = None) -> str | None:
    from app.airports import meta

    info = meta(code)
    is_national = bool(info["national"]) if national is None else bool(national)
    dest, _ = _load_indexes()
    order = ("national", "international") if is_national else ("international", "national")
    for key in destination_keys(code, city or str(info.get("city") or "")):
        for bucket in order:
            found = dest[bucket].get(key)
            if found:
                return found
    return None


def program_logo_url(program: str | None = None, airline: str | None = None) -> str | None:
    _, logos = _load_indexes()
    tokens = [fold_stem(program or ""), fold_stem(airline or "")]
    prog = fold_stem(program or "")
    if prog in LOGO_ALIASES:
        tokens.extend(LOGO_ALIASES[prog])
    air = fold_stem(airline or "")
    if "gol" in air:
        tokens.extend(LOGO_ALIASES["smiles"])
    if "latam" in air:
        tokens.extend(LOGO_ALIASES["latam"])
    if "azul" in air:
        tokens.extend(LOGO_ALIASES["azul"])
    seen: set[str] = set()
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        found = logos.get(token)
        if found:
            return found
    return None
