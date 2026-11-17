"""算子 5 测试公共 fixture：临时输出根、样例素材包、TestClient。

约定（DEVELOPMENT_CONTRACT §6）：
- ``client`` 与 ``uploaded_fixture`` 由本文件提供，``uploaded_fixture`` 保证含合法业务输入；
- 所有测试写入独立临时 ``output_root``，绝不污染仓库内 ``output/``；
- 在导入 ``app`` 之前注入 ``VIDEO_KEYFRAMES_CONFIG``，保证 HTTP 与 CLI 使用同一加载函数。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Mapping, Union

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE_DIR = ROOT / "dataset" / "sample"
SESSION_DIR = Path(tempfile.mkdtemp(prefix="vkf_tests_"))
OUTPUT_ROOT = SESSION_DIR / "output"
CONFIG_PATH = SESSION_DIR / "config.yaml"

CONFIG_PATH.write_text(
    "\n".join([
        "operator:",
        "  name: video_keyframes",
        "  version: v1.0.0",
        "stage_id: S2",
        "enabled: true",
        "sample_fps: 2.0",
        "max_frames: 8",
        "max_duration_seconds: 60.0",
        "crop_black_borders: false",
        "probe_timeout_seconds: 15.0",
        "unsupported_vfr_policy: reject",
        "io:",
        "  output_root: '%s'" % OUTPUT_ROOT.as_posix(),
        "server:",
        "  max_files: 1000",
        "  max_unpacked_bytes: 67108864",
        "  max_response_bytes: 268435456",
    ]) + "\n",
    encoding="utf-8",
)

os.environ["VIDEO_KEYFRAMES_CONFIG"] = str(CONFIG_PATH)

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import inference as core  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils.transport import restore_file_object  # noqa: E402

BytesLike = Union[bytes, str]


def make_zip(files: Mapping[str, BytesLike]) -> bytes:
    """把 ``{相对路径: bytes|str}`` 打成 zip（测试用，不落盘）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            archive.writestr(name, data)
    return buffer.getvalue()


def read_sample_files(relative_paths: Iterable[str]) -> Dict[str, bytes]:
    return {relative: (SAMPLE_DIR / relative).read_bytes() for relative in relative_paths}


def sample_zip_bytes() -> bytes:
    """完整样例素材包（manifest + 入口 + 媒体 + 历史血缘）。"""
    files: Dict[str, bytes] = {}
    for path in sorted(SAMPLE_DIR.rglob("*")):
        if path.is_file():
            files[path.relative_to(SAMPLE_DIR).as_posix()] = path.read_bytes()
    return make_zip(files)


def write_config(directory: Path, **overrides) -> Path:
    """生成一份指向指定 output_root 的临时配置，用于限额外/边界测试。"""
    settings = {
        "output_root": (directory / "output").as_posix(),
        "max_files": 1000,
        "max_unpacked_bytes": 67108864,
        "max_response_bytes": 268435456,
    }
    settings.update(overrides)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "config.yaml"
    target.write_text(
        "\n".join([
            "operator:",
            "  name: video_keyframes",
            "  version: v1.0.0",
            "stage_id: S2",
        "enabled: true",
            "sample_fps: 2.0",
            "max_frames: 8",
            "max_duration_seconds: 60.0",
            "crop_black_borders: false",
            "probe_timeout_seconds: 15.0",
            "unsupported_vfr_policy: reject",
            "io:",
            "  output_root: '%s'" % settings["output_root"],
            "server:",
            "  max_files: %d" % settings["max_files"],
            "  max_unpacked_bytes: %d" % settings["max_unpacked_bytes"],
            "  max_response_bytes: %d" % settings["max_response_bytes"],
        ]) + "\n",
        encoding="utf-8",
    )
    return target


@pytest.fixture(scope="session")
def client():
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def output_root() -> Path:
    return OUTPUT_ROOT


@pytest.fixture()
def unique_id() -> str:
    return "vk_%s" % uuid.uuid4().hex[:10]


@pytest.fixture()
def uploaded_fixture(client, unique_id) -> str:
    """上传完整样例包后的 uniqueId（含合法输入与媒体）。"""
    response = client.post(
        "/uploadFile",
        params={"uniqueId": unique_id},
        files={"file": ("sample.zip", sample_zip_bytes(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0, response.text
    assert response.json()["data"]["entrypoint"] == "demo.jsonl"
    return unique_id


def upload_zip(client, unique_id: str, payload: bytes, filename: str = "sample.zip"):
    return client.post(
        "/uploadFile",
        params={"uniqueId": unique_id},
        files={"file": (filename, payload, "application/zip")},
    )


def predict(client, unique_id: str, is_last: bool, **extra):
    body = {"uniqueId": unique_id, "isLastOperator": is_last}
    body.update(extra)
    return client.post("/predict", json=body)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session():
    yield
    shutil.rmtree(SESSION_DIR, ignore_errors=True)


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
