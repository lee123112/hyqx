"""算子 #12 评分图文语义一致性 测试公共夹具。

关键点：
1. 在导入 app 之前把 OUTPUT_DIR 指向临时目录，保证单测不污染算子目录下的 output/；
2. 提供标准库写 PNG 的辅助函数与测试替身 ``StubEncoder``（明确的 test double，
   不是模型，不参与任何精度结论）；
3. 提供 ``write_jsonl`` helper 用于构造临时输入。
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

_TEST_OUTPUT_ROOT = Path(tempfile.mkdtemp(prefix="op12_test_output_"))
os.environ["OUTPUT_DIR"] = str(_TEST_OUTPUT_ROOT)

SAMPLE_DIR = BASE / "dataset" / "sample"
SAMPLE_JSONL = SAMPLE_DIR / "pairs.jsonl"


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


# --------------------------------------------------------------------- 媒体


def png_bytes(rgb: tuple = (60, 180, 60), width: int = 32, height: int = 24) -> bytes:
    """生成一张纯色 PNG 的字节流（标准库实现，避免给测试引入图像库依赖）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_png(path: Path, rgb: tuple = (60, 180, 60), width: int = 32,
              height: int = 24) -> Path:
    """用标准库写一张纯色 PNG 到磁盘。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(rgb, width, height))
    return path


def write_jsonl(path: Path, rows: Iterable) -> Path:
    """把 dict / 原始字符串混合的行写成 UTF-8 JSONL。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            line = row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
            handle.write(line + "\n")
    return path


def write_raw(path: Path, payload: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------------- 矩阵


def unit(vector: Sequence[float]) -> List[float]:
    total = sum(float(item) ** 2 for item in vector) ** 0.5
    return [float(item) / total for item in vector]


def vector_with_cosine(value: float) -> List[float]:
    """返回与 [1, 0] 余弦恰好等于 ``value`` 的二维向量。"""
    return unit([float(value), (1.0 - float(value) ** 2) ** 0.5])


class StubEncoder(object):
    """测试替身（test double）：按注册表返回预先给定的向量。

    它**不是模型**，只用来在测试中精确控制余弦值，从而验证分档、台账与守恒逻辑；
    生产路径永远不会拿到它（``inference`` 只在显式传入时才使用）。
    """

    name = "stub_test_double"
    degraded = False
    test_double = True

    def __init__(self, text_vectors: Optional[Dict[str, Sequence[float]]] = None,
                 image_vectors: Optional[Dict[str, Sequence[float]]] = None,
                 image_errors: Optional[Dict[str, str]] = None,
                 raise_on_call: bool = False) -> None:
        self.text_vectors = {key: list(value) for key, value in (text_vectors or {}).items()}
        self.image_vectors = {key: list(value) for key, value in (image_vectors or {}).items()}
        self.image_errors = dict(image_errors or {})
        self.raise_on_call = bool(raise_on_call)
        self.encoded_texts = 0
        self.encoded_images = 0

    def model_identity(self) -> dict:
        return {
            "name": "CLIP-ViT/B-32",
            "version": "ViT-B-32",
            "library": "stub_test_double",
            "weights": None,
            "device": "cpu",
            "degraded": False,
            "real_backend": False,
            "test_double": True,
        }

    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        if self.raise_on_call:
            raise RuntimeError("stub_encoder_failure")
        out = []
        for text in texts:
            if text not in self.text_vectors:
                raise KeyError("测试替身未注册该文本向量：%r" % text)
            self.encoded_texts += 1
            out.append(list(self.text_vectors[text]))
        return out

    def encode_images(self, paths: Sequence[Optional[Path]]):
        if self.raise_on_call:
            raise RuntimeError("stub_encoder_failure")
        vectors: List[Optional[List[float]]] = []
        errors: List[Optional[str]] = []
        for path in paths:
            key = Path(path).name if path is not None else None
            if key in self.image_errors:
                vectors.append(None)
                errors.append(self.image_errors[key])
                continue
            if key not in self.image_vectors:
                raise KeyError("测试替身未注册该图片向量：%r" % key)
            self.encoded_images += 1
            vectors.append(list(self.image_vectors[key]))
            errors.append(None)
        return vectors, errors


@pytest.fixture
def stub_encoder():
    return StubEncoder


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
