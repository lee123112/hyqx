"""Path and upload safety helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import settings
from .envelope import ApiError
from .ids import is_valid_id

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def validate_unique_id(unique_id: str) -> str:
    if not is_valid_id(unique_id):
        raise ApiError(400, "INVALID_UNIQUE_ID", "uniqueId 不合法")
    return unique_id


def safe_join(base: Path, *parts: str, must_exist: bool = False) -> Path:
    base = base.resolve()
    candidate = base.joinpath(*[str(p).lstrip("/\\") for p in parts]).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ApiError(400, "PATH_ESCAPE", "路径越界") from exc
    if must_exist and not candidate.exists():
        raise ApiError(404, "NOT_FOUND", "文件不存在")
    return candidate


def sanitize_filename(name: str, fallback: str = "upload.bin") -> str:
    name = os.path.basename(name or "").strip()
    name = _UNSAFE_FILENAME.sub("_", name)
    return name[:120] or fallback


def logical_path(path: Path, unique_id: str) -> str:
    """Return a container-style logical path; never expose host absolute paths."""
    try:
        rel = path.resolve().relative_to(settings.workspace_dir.resolve())
        return "/workspace/" + rel.as_posix()
    except ValueError:
        return f"/workspace/{unique_id}/" + path.name


def sanitize_text(text: str, extra_roots: list[Path] | None = None) -> str:
    """Strip host absolute paths from logs and responses."""
    value = str(text)
    roots = [settings.project_root, settings.data_dir, settings.workspace_dir]
    roots.extend(extra_roots or [])
    for root in sorted({str(r) for r in roots}, key=len, reverse=True):
        value = value.replace(root, "<runtime>")
        value = value.replace(root.replace("\\", "/"), "<runtime>")
    value = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", value)
    value = re.sub(r"/(?:home|root|tmp|var)/[^\s\"']+", "<path>", value)
    return value
