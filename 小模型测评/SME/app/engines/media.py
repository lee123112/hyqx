"""Shared media asset resolution for the image/audio/video engines.

The engines accept assets from three places:

* a local path (absolute, or relative to the project root / ``dataset/sample``);
* a business-asset URL ``/api/v1/datasets/{kind}/{datasetId}/assets/{path}``
  served from ``settings.datasets_dir``;
* a public ``http(s)`` URL, downloaded into memory with a hard size cap.

Nothing in this module writes host absolute paths into engine results.  Errors
are raised as :class:`AssetUnavailable` so each engine decides whether the
current call is ``strict`` (HTTP 422 ``ASSET_MISSING``) or should record a
``missing`` row and continue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from ..core.config import settings
from ..core.envelope import ApiError
from ..core.security import safe_join

MAX_REMOTE_BYTES = 40 * 1024 * 1024
REMOTE_TIMEOUT_SECONDS = 10
_USER_AGENT = "sme-eval-engine/1.0"

_DRIVE_PATH = re.compile(r"^/[A-Za-z]:[\\/]")


class AssetUnavailable(Exception):
    """Raised when a media asset cannot be found, read or decoded."""

    def __init__(self, message: str, *, reason: str = "ASSET_MISSING") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass(frozen=True)
class AssetRef:
    """A resolved media location.

    ``kind`` is ``local`` or ``remote``.  ``location`` is a host path for local
    assets and a URL for remote ones; it is never copied into a result payload.
    """

    kind: str
    location: str
    label: str


def _sample_path(sample: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    for key in keys:
        for holder in (sample, parameters):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def sample_asset_value(sample: dict[str, Any]) -> str | None:
    """Return the media locator carried by a normalized evaluation sample."""
    return _sample_path(sample, ("uri", "input", "audio", "path", "assetPath", "fileName", "sourceFile"))


def sample_dataset_id(sample: dict[str, Any]) -> str | None:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    for holder in (sample, parameters):
        for key in ("datasetId", "dataset_id", "datasetID"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _looks_remote(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _remote_label(value: str) -> str:
    parts = urlsplit(value)
    name = Path(unquote(parts.path)).name
    return f"{parts.hostname or 'remote'}/{name}" if name else (parts.hostname or "remote")


def _local_label(value: str) -> str:
    name = Path(value).name
    return name or "local-asset"


def _resolve_api_asset(value: str, dataset_id: str | None) -> Path | None:
    parts = [part for part in unquote(urlsplit(value).path).split("/") if part]
    if "datasets" not in parts:
        return None
    index = parts.index("datasets")
    tail = parts[index + 1:]
    kind = tail[0] if len(tail) > 0 else ""
    dsid = tail[1] if len(tail) > 1 and tail[1] != "assets" else (dataset_id or "")
    if not dsid:
        return None
    if len(tail) > 2 and tail[2] == "assets":
        relative = tail[3:]
    else:
        # Tolerate /api/v1/datasets/{id}/file/{path} style variants.
        relative = tail[2:] if len(tail) > 2 else []
    if not relative:
        return None
    bases = []
    if kind:
        bases.append(settings.datasets_dir / kind / dsid / "assets")
        bases.append(settings.datasets_dir / kind / dsid)
    bases.append(settings.datasets_dir / dsid / "assets")
    bases.append(settings.datasets_dir / dsid)
    for base in bases:
        try:
            candidate = safe_join(base, *relative)
        except ApiError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _file_uri_path(value: str) -> Path:
    parts = urlsplit(value)
    path = unquote(parts.path)
    if _DRIVE_PATH.match(path):
        path = path[1:]
    if parts.netloc and not _DRIVE_PATH.match(path):
        path = f"//{parts.netloc}{path}"
    return Path(path)


def resolve_asset(value: str | None, *, dataset_id: str | None = None) -> AssetRef:
    """Resolve a sample locator to a readable local path or remote URL."""
    if not isinstance(value, str) or not value.strip():
        raise AssetUnavailable("未提供媒体资产路径")
    text = value.strip()
    lowered = text.lower()
    if text.startswith("/workspace/"):
        relative = unquote(text[len("/workspace/"):]).replace("\\", "/")
        if ".." in relative.split("/"):
            raise AssetUnavailable("工作区路径越界", reason="PATH_ESCAPE")
        try:
            path = safe_join(settings.workspace_dir, relative, must_exist=True)
        except ApiError as exc:
            raise AssetUnavailable("工作区资产不可用", reason=exc.error_code) from exc
        if not path.is_file():
            raise AssetUnavailable("工作区资产必须是文件")
        return AssetRef("local", str(path), path.name)
    if lowered.startswith("demo://"):
        raise AssetUnavailable("Demo 样本没有真实媒体资产")
    if _looks_remote(text):
        # An absolute URL may still point at this service's own dataset asset
        # endpoint; prefer the local file so a worker never needs an HTTP
        # self-call (and offline runs keep working).
        local = _resolve_api_asset(urlsplit(text).path, dataset_id)
        if local is not None:
            return AssetRef("local", str(local), local.name)
        return AssetRef("remote", text, _remote_label(text))
    if text.startswith("/api/v1/") or text.startswith("api/v1/"):
        path = _resolve_api_asset(text if text.startswith("/") else "/" + text, dataset_id)
        if path is None:
            raise AssetUnavailable("资产 URL 未在数据集目录中找到", reason="ASSET_MISSING")
        return AssetRef("local", str(path), path.name)
    if lowered.startswith("file://"):
        path = _file_uri_path(text)
        if path.is_file():
            return AssetRef("local", str(path), path.name)
        raise AssetUnavailable("本地文件不存在，资产缺失")
    if lowered.startswith("local://"):
        text = text[len("local://"):]
    direct = Path(text)
    candidates = [direct]
    if not direct.is_absolute():
        candidates.extend([settings.project_root / text, settings.sample_dir / text])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return AssetRef("local", str(candidate), _local_label(text))
        except OSError:
            continue
    raise AssetUnavailable("本地文件不存在，资产缺失")


def read_asset_bytes(ref: AssetRef, *, max_bytes: int = MAX_REMOTE_BYTES) -> bytes:
    """Read an asset into memory with a hard byte cap for remote URLs."""
    if ref.kind == "local":
        try:
            size = Path(ref.location).stat().st_size
            if size > max_bytes:
                raise AssetUnavailable(f"媒体文件超过 {max_bytes // (1024 * 1024)}MB 上限")
            return Path(ref.location).read_bytes()
        except AssetUnavailable:
            raise
        except OSError as exc:
            raise AssetUnavailable(f"媒体文件读取失败：{ref.label}", reason="ASSET_MISSING") from exc

    request = Request(ref.location, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS) as response:  # noqa: S310
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise AssetUnavailable(f"远程媒体超过 {max_bytes // (1024 * 1024)}MB 上限")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise AssetUnavailable(f"远程媒体超过 {max_bytes // (1024 * 1024)}MB 上限")
            if not payload:
                raise AssetUnavailable("远程媒体为空")
            return payload
    except AssetUnavailable:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise AssetUnavailable(f"远程媒体下载失败：{ref.label}", reason="ASSET_MISSING") from exc


def load_asset_bytes(value: str | None, *, dataset_id: str | None = None,
                     max_bytes: int = MAX_REMOTE_BYTES) -> tuple[bytes, str]:
    """Resolve and read an asset; return ``(payload, label_without_host_path)``."""
    ref = resolve_asset(value, dataset_id=dataset_id)
    return read_asset_bytes(ref, max_bytes=max_bytes), ref.label


def missing_rows(sample_id: str, metric: str, reason: str, detail: str,
                 models: tuple[str, ...] = ("baseline", "candidate")) -> list[dict[str, Any]]:
    """Build contract-shaped ``missing`` rows for both model sides."""
    return [
        {"sampleId": sample_id, "model": model, "metric": metric,
         "reason": reason, "detail": detail}
        for model in models
    ]


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if value != value:  # NaN
        return low
    return max(low, min(high, value))


def round_metric(value: float, digits: int = 4) -> float:
    return round(float(value), digits)
