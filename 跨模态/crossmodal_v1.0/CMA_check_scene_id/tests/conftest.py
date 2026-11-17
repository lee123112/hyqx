"""算子 #02 测试夹具。

夹具只依赖仓库内样例与临时合成数据，不下载任何外部数据集，不访问网络。
"""
from __future__ import annotations
import tempfile
import os

import sys
from pathlib import Path

import pytest

OPERATOR_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = OPERATOR_DIR / "dataset" / "sample"
TESTS_DIR = Path(__file__).resolve().parent

for _path in (str(OPERATOR_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tests.support import zip_directory  # noqa: E402  (依赖上面的 sys.path)

TEST_DEFAULT_CONFIG = OPERATOR_DIR / "configs" / "config.yaml"


@pytest.fixture(scope="session")
def operator_dir() -> Path:
    return OPERATOR_DIR


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    """确保 dataset/sample 样例存在（确定性生成，可重复）。"""
    import importlib.util

    module_path = SAMPLE_DIR / "make_sample.py"
    spec = importlib.util.spec_from_file_location("op02_make_sample", str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_sample(SAMPLE_DIR)
    return SAMPLE_DIR


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def client(output_root, monkeypatch):
    """TestClient：OUTPUT_DIR 指向临时目录，避免污染仓库 output/。"""
    monkeypatch.setenv("OUTPUT_DIR", str(output_root))
    import app as app_module
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


@pytest.fixture()
def upload(client, tmp_path, sample_dir):
    """上传夹具：默认上传仓库样例包，可指定自定义目录或已存在的 zip。"""

    def _upload(unique_id: str, directory=None, archive=None, entry="demo.jsonl"):
        if archive is None:
            directory = Path(directory) if directory is not None else sample_dir
            archive = tmp_path / ("%s_bundle.zip" % unique_id)
            zip_directory(directory, archive)
        with open(str(archive), "rb") as stream:
            return client.post(
                "/uploadFile",
                params={"uniqueId": unique_id},
                files={"file": ("bundle.zip", stream, "application/zip")},
            )

    return _upload


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
