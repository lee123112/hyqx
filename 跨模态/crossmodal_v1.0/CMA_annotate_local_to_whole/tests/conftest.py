"""pytest 公共夹具：算子目录入 sys.path、独立输出根、TestClient、配置写入器。"""
from __future__ import annotations
import tempfile

import os
import sys
from pathlib import Path

import pytest
import yaml

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"


@pytest.fixture()
def operator_dir() -> Path:
    return BASE_DIR


@pytest.fixture()
def config_path() -> Path:
    return CONFIG_PATH


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    """每次测试独立输出根，避免任务目录互相污染。"""
    root = tmp_path / "output"
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    return root


@pytest.fixture()
def config_writer(tmp_path):
    """把业务参数写进独立的 YAML 文件，返回其路径。"""
    counter = {"index": 0}

    def _write(**overrides) -> Path:
        counter["index"] += 1
        payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        payload.update(overrides)
        target = tmp_path / ("config_%d.yaml" % counter["index"])
        target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return target

    return _write


@pytest.fixture()
def enabled_overrides():
    return {"enabled": True}


@pytest.fixture()
def client(output_root, monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    monkeypatch.setattr(app_module, "output_root", lambda: output_root)
    return TestClient(app_module.app)


@pytest.fixture()
def uploaded_client(client):
    """客户端 + 一个已上传标准素材包的任务工厂。"""

    def _upload(unique_id: str = "api_task_1", payload=None, filename: str = "bundle.zip"):
        body = payload if payload is not None else _default_bundle()
        response = client.post("/uploadFile", params={"uniqueId": unique_id},
                               files={"file": (filename, body, "application/zip")})
        assert response.status_code == 200, response.text
        return response.json()

    return _upload


def _default_bundle():
    from tests import fixtures

    return fixtures.bundle_zip()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OPERATOR_CONFIG", raising=False)
    yield
    os.environ.pop("OPERATOR_CONFIG", None)


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
