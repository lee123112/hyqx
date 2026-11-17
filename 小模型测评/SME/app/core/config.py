"""Runtime configuration for the small-model evaluation service."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dataset_root() -> Path:
    """Keep the operator package clean while allowing an external dataset root.

    ``SME/dataset`` is preferred when it exists (legacy/local layout).  The
    normal delivery layout keeps fixtures and downloads outside the package at
    ``<project>/dataset``; test1 is one implementation of that layout.
    """
    configured = (os.environ.get("SME_DATASET_DIR") or "").strip()
    if configured:
        return Path(configured).resolve()
    local = _project_root() / "dataset"
    if local.is_dir():
        return local
    return _project_root().parent / "dataset"


@dataclass(frozen=True)
class Settings:
    project_root: Path = field(default_factory=_project_root)
    host: str = os.environ.get("SME_HOST", "0.0.0.0")
    port: int = _env_int("SME_PORT", 6289)

    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("SME_DATA_DIR", str(_project_root() / "data"))).resolve())
    workspace_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("SME_WORKSPACE_DIR", str(_project_root() / "data" / "workspace"))).resolve())
    db_path: Path = field(default_factory=lambda: Path(
        os.environ.get("SME_DB_PATH", str(_project_root() / "data" / "evaluation.db"))).resolve())
    templates_path: Path = field(default_factory=lambda: _project_root() / "configs" / "evaluation_templates.json")
    dataset_dir: Path = field(default_factory=_dataset_root)
    sample_dir: Path = field(default_factory=lambda: _dataset_root() / "sample")

    max_samples: int = _env_int("SME_MAX_SAMPLES", 5000)
    max_upload_mb: int = _env_int("SME_MAX_UPLOAD_MB", 200)
    max_zip_files: int = _env_int("SME_MAX_ZIP_FILES", 5000)
    demo_step_seconds: float = _env_float("SME_DEMO_STEP_SECONDS", 0.05)
    max_workers: int = _env_int("SME_MAX_WORKERS", 2)

    cors_origins: str = os.environ.get(
        "SME_CORS_ORIGINS",
        "http://127.0.0.1:6289,http://localhost:6289,"
        "https://mty-data-production-platform.jwrs5157.chatgpt.site",
    )
    api_key: str = os.environ.get("SME_API_KEY", "")
    allow_demo_fallback: bool = os.environ.get("SME_ALLOW_DEMO_FALLBACK", "0") == "1"

    text_model_path: str = os.environ.get("SME_TEXT_MODEL_PATH", "")
    whisper_model: str = os.environ.get("SME_WHISPER_MODEL", "")
    paraformer_model: str = os.environ.get("SME_PARAFORMER_MODEL", "")
    clip_model: str = os.environ.get("SME_CLIP_MODEL", "")
    engine_enable_torch: bool = os.environ.get("SME_ENGINE_ENABLE_TORCH", "1") == "1"

    log_max_chars: int = 160

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspace_dir, self.datasets_dir,
                     self.artifacts_dir, self.runs_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
