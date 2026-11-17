"""Business identifiers compatible with the platform frontend."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
SHANGHAI = timezone(timedelta(hours=8))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def date_part(ts: datetime | None = None) -> str:
    return (ts or datetime.now(SHANGHAI)).strftime("%y%m%d")


def random_code(length: int = 6) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def business_id(prefix: str = "EVAL") -> str:
    return f"{prefix}{date_part()}{random_code()}"


def new_task_id() -> str:
    return business_id("EVAL")


def new_dataset_id() -> str:
    return business_id("DS")


def feedback_id(task_id: str, index: int) -> str:
    return f"{task_id}-F{index}"


def is_valid_id(value: str) -> bool:
    return bool(value) and bool(ID_RE.match(value))
