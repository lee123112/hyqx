"""算子 20「质检对齐产物」测试夹具。

**全部媒体均由脚本真实合成**（PNG 用 zlib 手写、WAV 用标准库 ``wave``、
MP4 用本机 OpenCV ``mp4v`` 编码器），不下载任何外部数据，保证可复现。

同时提供：
- ``business()``：业务参数字典（与 ``utils/alignment_qc.default_business`` 同源）；
- ``StubEncoder``：**明确标注的测试替身**编码器，只用于验证反向检索管线，
  不会出现在任何正式运行结果里（正式运行只接受本地权重实例）。
"""
from __future__ import annotations
import tempfile
import os

import json
import struct
import wave
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pytest


# --------------------------------------------------------------------------- #
# 真实媒体合成
# --------------------------------------------------------------------------- #

def png_bytes(width: int = 32, height: int = 24, rgb=(200, 40, 40)) -> bytes:
    """生成一张真实可解码的 PNG（纯色真彩）。"""
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_png(path: Path, width: int = 32, height: int = 24, rgb=(200, 40, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(width, height, rgb))
    return path


def write_wav(path: Path, seconds: float = 0.25, freq: float = 440.0,
              samplerate: int = 16000, silent: bool = False,
              amplitude: int = 6000) -> Path:
    """生成 16bit 单声道 PCM WAV；``silent=True`` 时写全 0（整段无声）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, int(seconds * samplerate))
    if silent:
        samples = [0] * total
    else:
        samples = [int(amplitude * np.sin(2 * np.pi * freq * i / samplerate))
                   for i in range(total)]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(samplerate)
        handle.writeframes(struct.pack("<%dh" % total, *samples))
    return path


def write_mixed_wav(path: Path, loud_seconds: float = 0.3,
                    quiet_seconds: float = 0.1, freq: float = 440.0,
                    samplerate: int = 16000, loud: int = 8000,
                    quiet: int = 20) -> Path:
    """响亮段 + 安静段的真实 WAV，用于验证 SNR 是真算出来的（> 0 dB）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [int(loud * np.sin(2 * np.pi * freq * i / samplerate))
               for i in range(int(loud_seconds * samplerate))]
    samples += [int(quiet * np.sin(2 * np.pi * freq * i / samplerate))
                for i in range(int(quiet_seconds * samplerate))]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(samplerate)
        handle.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return path


def write_mp4(path: Path, frames: int = 8, width: int = 64, height: int = 48) -> Path:
    """用本机 OpenCV ``mp4v`` 写一段真实可解码的 MP4。"""
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0,
                             (width, height))
    if not writer.isOpened():  # pragma: no cover - 环境缺少 mp4v 编码器
        pytest.skip("本机 OpenCV 无法写出 mp4v，跳过视频相关用例")
    try:
        for index in range(frames):
            frame = np.full((height, width, 3), (index * 17) % 256, dtype=np.uint8)
            frame[:, :, 1] = (index * 37) % 256
            writer.write(frame)
    finally:
        writer.release()
    return path


def write_broken(path: Path, payload: bytes = b"this-is-not-a-valid-media") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
             for item in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 媒体目录夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def media_root(tmp_path: Path) -> Path:
    """**媒体清单根目录**（即入口 JSONL 所在目录），媒体放在其 ``media/`` 子目录。

    样本中的相对路径（如 ``media/ok_a.png``）就是相对这个根解析的，
    与真实上传包的结构一致。返回根目录本身，因此测试里可直接用
    ``tmp_path`` 语义引用媒体：``media_root / "media" / "ok_a.png"``。
    """
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    write_png(media / "ok_a.png", rgb=(210, 40, 40))
    write_png(media / "ok_b.png", rgb=(40, 210, 40))
    write_png(media / "ok_c.png", rgb=(40, 40, 210))
    write_broken(media / "broken.png")
    write_wav(media / "tone.wav")
    write_mixed_wav(media / "mixed.wav")
    write_wav(media / "silent.wav", silent=True)
    write_broken(media / "broken.wav")
    write_mp4(media / "clip_a.mp4")
    write_broken(media / "broken.mp4")
    return tmp_path


# --------------------------------------------------------------------------- #
# 业务参数
# --------------------------------------------------------------------------- #

def business(**overrides) -> Dict[str, Any]:
    """与部署配置同源的业务参数字典（避免测试与实现各写一套默认值）。"""
    from utils import alignment_qc

    return alignment_qc.default_business(**overrides)


@pytest.fixture()
def biz() -> Dict[str, Any]:
    return business()


# --------------------------------------------------------------------------- #
# 明确标注的测试替身编码器（不会出现在正式运行结果里）
# --------------------------------------------------------------------------- #

class StubEncoder:
    """确定性测试替身：向量由文本/图像字节的稳定哈希派生。

    **只用于验证反向检索管线的排序与分档逻辑**，不携带任何语义能力，
    因此不用于断言任何精度或分数。正式运行不接受该实现。
    """

    name = "stub-encoder"
    version = "test-only"
    is_test_double = True

    def __init__(self, dim: int = 16, fail_paths: Optional[Sequence[str]] = None,
                 vectors: Optional[Dict[str, List[float]]] = None) -> None:
        self.dim = dim
        self.fail_paths = set(fail_paths or ())
        self.vectors = dict(vectors or {})

    # -- 内部 ------------------------------------------------------------- #
    def _vector(self, key: str) -> np.ndarray:
        if key in self.vectors:
            vector = np.asarray(self.vectors[key], dtype=np.float64)
        else:
            digest = np.frombuffer(key.encode("utf-8"), dtype=np.uint8).astype(np.float64)
            vector = np.resize(digest, self.dim)
            vector = vector + np.arange(self.dim, dtype=np.float64) * 0.01
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector

    # -- 接口 ------------------------------------------------------------- #
    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        return np.vstack([self._vector("text:%s" % text) for text in texts])

    def encode_images(self, paths: Sequence[Path]) -> List[Optional[np.ndarray]]:
        result: List[Optional[np.ndarray]] = []
        for path in paths:
            if str(path) in self.fail_paths or path.name in self.fail_paths:
                result.append(None)
                continue
            result.append(self._vector("image:%s" % path.name))
        return result


@pytest.fixture()
def stub_encoder() -> StubEncoder:
    return StubEncoder()


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
