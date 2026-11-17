"""算子 #11 测试公共夹具。

关键点：在导入 app 之前把 OUTPUT_DIR 指向临时目录，
保证单测不污染算子目录下的 output/（交付要求 output/ 保持为空）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

_TEST_OUTPUT_ROOT = Path(tempfile.mkdtemp(prefix="op11_test_output_"))
os.environ["OUTPUT_DIR"] = str(_TEST_OUTPUT_ROOT)

SAMPLE_DIR = BASE / "dataset" / "sample"
SAMPLE_JSONL = SAMPLE_DIR / "samples.jsonl"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TEST_OUTPUT_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def base_dir() -> Path:
    return BASE


@pytest.fixture(scope="session")
def sample_jsonl() -> Path:
    assert SAMPLE_JSONL.is_file(), "缺少样例，请先运行 dataset/sample/make_sample.py"
    return SAMPLE_JSONL


@pytest.fixture(scope="session")
def test_output_root() -> Path:
    return _TEST_OUTPUT_ROOT


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
