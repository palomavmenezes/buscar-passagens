from app.collectors.azul import azul_session_ready, collect_azul_jobs, open_azul_login
from app.collectors.latam import collect_latam_jobs, confirm_latam_session, latam_session_ready, open_latam_login
from app.collectors.smiles import collect_smiles_jobs, open_smiles_login, smiles_session_ready

__all__ = [
    "azul_session_ready",
    "collect_azul_jobs",
    "collect_latam_jobs",
    "collect_smiles_jobs",
    "confirm_latam_session",
    "latam_session_ready",
    "open_azul_login",
    "open_latam_login",
    "open_smiles_login",
    "smiles_session_ready",
]
