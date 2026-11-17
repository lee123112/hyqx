"""pytest 公共夹具：样例生成、隔离输出根、HTTP 客户端。"""
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

SAMPLE_ENTRY = BASE_DIR / "dataset" / "sample" / "demo.jsonl"


@pytest.fixture(scope="session", autouse=True)
def ensure_sample_dataset() -> Path:
    """样例缺失时用 make_sample.py 真实生成（不下载外部数据）。"""
    if not SAMPLE_ENTRY.is_file():
        subprocess.run([sys.executable, str(BASE_DIR / "dataset" / "sample" / "make_sample.py")],
                       check=True, cwd=str(BASE_DIR))
    return SAMPLE_ENTRY


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    """隔离输出根：请求不可指定，只由 OUTPUT_DIR 决定。"""
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    return root


@pytest.fixture()
def client(output_root):
    """FastAPI TestClient；``app.output_root()`` 每次请求读取环境变量。"""
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
