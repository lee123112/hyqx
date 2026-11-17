"""pytest 公共装置：编码兜底、fixture 生成、任务铺盘与 TestClient。"""
from __future__ import annotations
import tempfile
import os

import importlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Windows 控制台默认 GBK，先兜底为 UTF-8 以免 emoji/CJK 打印中断测试
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - 无法重配时继续
        pass

SAMPLE_DIR = BASE_DIR / "dataset" / "sample"


@pytest.fixture(scope="session", autouse=True)
def fixtures_ready():
    """确保本地 fixture 已生成（测试不依赖上游算子产物）。"""
    sys.path.insert(0, str(SAMPLE_DIR))
    make_sample = importlib.import_module("make_sample")
    if not (SAMPLE_DIR / "demo.jsonl").is_file():
        make_sample.write_fixtures()
    return SAMPLE_DIR


@pytest.fixture()
def sample_dir() -> Path:
    return SAMPLE_DIR


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    """每次测试独立的输出根，通过 OUTPUT_DIR 传给算子。"""
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    return root


@pytest.fixture()
def stage():
    """把 fixture 素材铺成 ``<root>/<uniqueId>/input``，返回任务目录。"""

    def _stage(root: Path, unique_id: str, entry: str = "demo.jsonl",
               with_manifest: bool = True, with_history: bool = True) -> Path:
        input_dir = Path(root) / unique_id / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True)
        shutil.copyfile(SAMPLE_DIR / entry, input_dir / entry)
        if with_manifest:
            shutil.copyfile(SAMPLE_DIR / "manifest.json", input_dir / "manifest.json")
        shutil.copytree(SAMPLE_DIR / "media", input_dir / "media")
        if with_history:
            shutil.copyfile(SAMPLE_DIR / "lineage_history.json",
                            input_dir / "lineage_history.json")
        return Path(root) / unique_id

    return _stage


@pytest.fixture()
def client(output_root):
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


def make_zip(source_dir: Path, archive: Path, extra_entries=None) -> Path:
    """把目录打包成 zip，可附加自定义成员（用于路径逃逸等负例）。"""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(Path(source_dir).rglob("*")):
            if path.is_file():
                zf.write(path, str(path.relative_to(source_dir)).replace("\\", "/"))
        for name, payload, attrs in (extra_entries or []):
            info = zipfile.ZipInfo(name)
            info.external_attr = attrs
            zf.writestr(info, payload)
    return archive


def read_jsonl(path: Path):
    records = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
