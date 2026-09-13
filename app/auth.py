from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from fastapi import Request

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PBKDF2_ROUNDS = 260_000


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value)) and len(value) <= 120


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ROUNDS,
    ).hex()
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(rounds),
    ).hex()
    return hmac.compare_digest(check, digest)


def safe_next_path(value: str | None) -> str:
    path = (value or "").strip()
    if path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


def current_user(request: Request) -> dict[str, Any] | None:
    user = request.session.get("user")
    if isinstance(user, dict) and user.get("id"):
        return user
    return None


def is_admin(request: Request) -> bool:
    user = current_user(request)
    return bool(user and user.get("role") == "admin")


def is_approved(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return user.get("status") == "approved"
