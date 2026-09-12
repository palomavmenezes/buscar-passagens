from pathlib import Path
import os

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "passagens.db"

APP_PASSWORD = os.getenv("APP_PASSWORD", "viannas-2026")
SECRET_KEY = os.getenv("SECRET_KEY", "mude-esta-chave")
GECKOAPI_API_KEY = os.getenv("GECKOAPI_API_KEY", "").strip()

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))

DEFAULT_ORIGINS = ["GIG", "SDU", "VIX", "GRU"]
MAX_FLEX_DAYS = 180
MAX_YEAR_DAYS = 365
DATE_MODES = ("flex", "year", "month", "specific")

_harvest_raw = os.getenv("HARVEST_DIR", "").strip()
if _harvest_raw:
    _harvest_path = Path(_harvest_raw).expanduser()
    HARVEST_DIR = (
        _harvest_path if _harvest_path.is_absolute() else (ROOT / _harvest_path).resolve()
    )
else:
    HARVEST_DIR = DATA_DIR / "harvest"
HARVEST_DIR.mkdir(parents=True, exist_ok=True)
HARVEST_HOURS = tuple(
    int(part.strip())
    for part in os.getenv("HARVEST_HOURS", "0,12").split(",")
    if part.strip().isdigit()
) or (0, 12)
HARVEST_MAX_REQUESTS = max(1, int(os.getenv("HARVEST_MAX_REQUESTS", "4")))
HARVEST_CREDIT_COST = max(1, int(os.getenv("HARVEST_CREDIT_COST", "5")))
HARVEST_CREDIT_RESERVE = max(0, int(os.getenv("HARVEST_CREDIT_RESERVE", "10")))
HARVEST_ORIGINS = [
    code.strip().upper()
    for code in os.getenv("HARVEST_ORIGINS", "GIG,GRU").split(",")
    if code.strip()
]
HARVEST_DESTINATIONS = [
    code.strip().upper()
    for code in os.getenv("HARVEST_DESTINATIONS", "SSA,FOR,REC,LIS,EZE").split(",")
    if code.strip()
]
HARVEST_PROGRAMS = [
    name.strip().lower()
    for name in os.getenv("HARVEST_PROGRAMS", "smiles,azul,latam").split(",")
    if name.strip()
]
HARVEST_DATE_OFFSETS = [
    int(part.strip())
    for part in os.getenv("HARVEST_DATE_OFFSETS", "30,90").split(",")
    if part.strip().isdigit()
] or [30, 90]
HARVEST_TZ = os.getenv("HARVEST_TZ", "America/Sao_Paulo")
LATAM_PROFILE_DIR = DATA_DIR / "latam-chrome-profile"
AZUL_PROFILE_DIR = DATA_DIR / "azul-chrome-profile"
SMILES_PROFILE_DIR = DATA_DIR / "smiles-chrome-profile"
LATAM_MAX_ROUTES = max(1, int(os.getenv("LATAM_MAX_ROUTES", "6")))
LATAM_MAX_PER_RUN = max(1, int(os.getenv("LATAM_MAX_PER_RUN", "6")))
LATAM_PAUSE_SECONDS = float(os.getenv("LATAM_PAUSE_SECONDS", "60"))
LATAM_COOLDOWN_SECONDS = float(os.getenv("LATAM_COOLDOWN_SECONDS", "180"))
LATAM_BATCH_GAP_SECONDS = float(os.getenv("LATAM_BATCH_GAP_SECONDS", "600"))
LATAM_HEADLESS = os.getenv("LATAM_HEADLESS", "0") == "1"
CIA_HEADLESS = os.getenv("CIA_HEADLESS", os.getenv("LATAM_HEADLESS", "0")) == "1"
CIA_PAUSE_SECONDS = float(os.getenv("CIA_PAUSE_SECONDS", os.getenv("LATAM_PAUSE_SECONDS", "60")))

CABIN_LABELS = {
    "economy": "Econômica",
    "premium": "Premium economy",
    "business": "Executiva",
    "first": "Primeira",
    "all": "Todas",
}

PROGRAM_LABELS = {
    "aeroplan": "Aeroplan",
    "alaska": "Alaska Atmos",
    "american": "AAdvantage",
    "aeromexico": "Aeroméxico",
    "lifemiles": "LifeMiles",
    "azul": "TudoAzul",
    "latam": "LATAM Pass",
    "copa": "ConnectMiles",
    "delta": "SkyMiles",
    "emirates": "Skywards",
    "ethiopian": "ShebaMiles",
    "etihad": "Etihad Guest",
    "finnair": "Finnair Plus",
    "flyingblue": "Flying Blue",
    "smiles": "GOL Smiles",
    "jetblue": "TrueBlue",
    "lufthansa": "Miles & More",
    "qantas": "Qantas",
    "qatar": "Privilege Club",
    "eurobonus": "EuroBonus",
    "saudia": "AlFursan",
    "singapore": "KrisFlyer",
    "turkish": "Miles&Smiles",
    "united": "MileagePlus",
    "virginatlantic": "Virgin Atlantic",
    "velocity": "Velocity",
}


def has_miles_provider() -> bool:
    return True


def has_live_cia_miles() -> bool:
    return any(
        (folder / ".logged_in").exists()
        for folder in (LATAM_PROFILE_DIR, AZUL_PROFILE_DIR, SMILES_PROFILE_DIR)
    )
