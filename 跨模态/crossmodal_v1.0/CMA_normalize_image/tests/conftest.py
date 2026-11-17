"""pytest 夹具：把 ``dataset/sample`` 拷进临时任务目录，隔离真实 output/。"""
from __future__ import annotations
import tempfile
import os

import shutil
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

SAMPLE_DIR = BASE_DIR / "dataset" / "sample"
UNIQUE_ID = "demoS2_f1"
SAMPLE_FILES = (
    "demo.jsonl",
    "manifest.json",
    "lineage_history.json",
    "input_empty.jsonl",
    "input_bad_encoding.jsonl",
)


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    assert (SAMPLE_DIR / "demo.jsonl").is_file(), "请先运行 dataset/sample/make_sample.py"
    return SAMPLE_DIR


@pytest.fixture()
def staged(tmp_path, sample_dir):
    """在 ``<tmp>/output/<uniqueId>/input`` 下铺好完整素材包。

    返回 ``(root, unique_id, entry_path)``；``root`` 为输出根目录。
    """
    root = tmp_path / "output"
    input_dir = root / UNIQUE_ID / "input"
    input_dir.mkdir(parents=True)
    for name in SAMPLE_FILES:
        shutil.copyfile(sample_dir / name, input_dir / name)
    shutil.copytree(sample_dir / "media", input_dir / "media")
    return root, UNIQUE_ID, input_dir / "demo.jsonl"


@pytest.fixture()
def run_dirs(staged):
    """返回 ``(root, unique_id, entry, run_dir 解析器)`` 便于按 runId 定位运行目录。"""

    def locate(result) -> Path:
        root, unique_id, _entry = staged
        return (root / unique_id / "image_standardization" / result["run_id"]).resolve()

    root, unique_id, entry = staged
    return root, unique_id, entry, locate


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
