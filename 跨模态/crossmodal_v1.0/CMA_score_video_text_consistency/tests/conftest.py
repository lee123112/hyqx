"""算子 14 测试公共设施：路径引导、真实 PNG 合成、配置夹具。

本文件只使用标准库；不下载任何数据、不加载任何远程模型。
"""
from __future__ import annotations
import tempfile
import os

import struct
import sys
import zlib
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(BASE), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def png_bytes(width: int = 32, height: int = 24, rgb=(200, 40, 40)) -> bytes:
    """用 zlib 合成一张**真实可解码**的 PNG（纯色，仅用于管线验证）。"""
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_png(path, width: int = 32, height: int = 24, rgb=(200, 40, 40)) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes(width=width, height=height, rgb=rgb))
    return target


DEFAULT_CONFIG_YAML = """\
operator:
  name: video_text_scoring
  version: v1.0.0
stage_id: S3
enabled: false
# [2026-11-05 全量标定] 与 configs/config.yaml 默认保持一致：0.20
low_score_threshold: 0.20
max_frames_per_segment: 8
max_text_chars: 500
allow_degraded: false
clip:
  library: auto
  model_name: ViT-B-32
  weights: null
  device: cpu
  batch_size: 8
"""


@pytest.fixture()
def config_file(tmp_path) -> Path:
    """默认配置副本（enabled=false，无权重）。"""
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def video_media():
    """三帧真实 PNG（供媒体搬运与 CLIP 解码路径使用）。"""
    return {
        "frames/frame_0001.png": png_bytes(rgb=(210, 40, 40)),
        "frames/frame_0002.png": png_bytes(rgb=(40, 210, 40)),
        "frames/frame_0003.png": png_bytes(rgb=(40, 40, 210)),
    }


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
