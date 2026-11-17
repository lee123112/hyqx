"""pytest 共享 fixture。"""
from __future__ import annotations
import tempfile
import os

import subprocess
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests import fixtures  # noqa: E402

SAMPLE_ENTRY = BASE_DIR / "dataset" / "sample" / "demo.jsonl"
SAMPLE_ENABLED_ENTRY = BASE_DIR / "dataset" / "sample" / "demo_enabled.jsonl"


@pytest.fixture(scope="session", autouse=True)
def ensure_sample_dataset() -> Path:
    """样例缺失时按脚本幂等重建。"""
    if not SAMPLE_ENTRY.is_file():
        subprocess.run([sys.executable, str(BASE_DIR / "dataset" / "sample" / "make_sample.py")],
                       cwd=str(BASE_DIR), check=True)
    return SAMPLE_ENTRY


@pytest.fixture()
def operator_dir() -> Path:
    return BASE_DIR


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    return root


@pytest.fixture()
def client(output_root, monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    monkeypatch.setenv("OUTPUT_DIR", str(output_root))
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def tiny_models(tmp_path_factory) -> dict:
    """会话级复用：真实 transformers 前向所需的微型本地权重。"""
    root = tmp_path_factory.mktemp("tiny_models")
    return fixtures.build_tiny_models(root)


@pytest.fixture()
def tiny_config_path(tmp_path, tiny_models) -> Path:
    """启用音文路线并指向微型本地权重的配置。"""
    path = tmp_path / "config_tiny.yaml"
    path.write_text(
        "\n".join([
            "operator:",
            "  name: audio_text_scoring",
            "  version: v1.0.0",
            "stage_id: S3",
            "enabled: true",
            "edit_distance_ratio_max: 0.30",
            "model_root: %s" % tiny_models["root"].replace("\\", "/"),
            "audio_model_dir: %s" % tiny_models["audio_dir"],
            "text_model_dir: %s" % tiny_models["text_dir"],
            "sample_rate: 16000",
            "max_audio_seconds: 30.0",
            "max_text_chars: 500",
            "allow_degraded: false",
            "",
        ]),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def degraded_config_path(tmp_path) -> Path:
    """启用音文路线但没有本地权重，且允许显式降级。"""
    missing = tmp_path / "no_models_here"
    path = tmp_path / "config_degraded.yaml"
    path.write_text(
        "\n".join([
            "operator:",
            "  name: audio_text_scoring",
            "  version: v1.0.0",
            "stage_id: S3",
            "enabled: true",
            "edit_distance_ratio_max: 0.30",
            "model_root: %s" % str(missing).replace("\\", "/"),
            "audio_model_dir: wav2vec2-base",
            "text_model_dir: bert-base-chinese",
            "sample_rate: 16000",
            "max_audio_seconds: 30.0",
            "max_text_chars: 500",
            "allow_degraded: true",
            "",
        ]),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def strict_config_path(tmp_path) -> Path:
    """启用音文路线、无权重、不允许降级（必须显式失败）。"""
    missing = tmp_path / "no_models_here"
    path = tmp_path / "config_strict.yaml"
    path.write_text(
        "\n".join([
            "operator:",
            "  name: audio_text_scoring",
            "  version: v1.0.0",
            "stage_id: S3",
            "enabled: true",
            "model_root: %s" % str(missing).replace("\\", "/"),
            "audio_model_dir: wav2vec2-base",
            "text_model_dir: bert-base-chinese",
            "allow_degraded: false",
            "",
        ]),
        encoding="utf-8",
    )
    return path


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
