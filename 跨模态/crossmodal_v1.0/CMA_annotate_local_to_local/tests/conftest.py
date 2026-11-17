"""算子 18 测试公共设施：路径引导、真实 PNG 合成、配置夹具与**测试替身编码器**。

重要说明（诚实边界）：
- 本文件只用标准库合成媒体，不下载任何外部数据、不加载任何远程权重；
- ``StubEncoder`` 是**明确标注的测试替身**：它只用于验证
  分词→裁图→余弦→一对一匹配→阈值分档→台账 的**管线与业务规则**，
  绝不代表 CLIP-ViT/B-32 的真实语义精度，也不会出现在任何正式运行结果里；
- 真实 CLIP 架构前向由 ``tests/test_clip_plumbing.py`` 单独验证（不联网、随机权重、
  只断言管线形状与单位化，不断言分数）。
"""
from __future__ import annotations
import tempfile
import os

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

BASE = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(BASE), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# 真实可解码 PNG/媒体合成（仅标准库）
# --------------------------------------------------------------------------- #

def png_bytes(width: int = 64, height: int = 48, rgb=(200, 40, 40)) -> bytes:
    """用 zlib 合成一张**真实可解码**的 PNG（纯色）。"""
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_png(path, width: int = 64, height: int = 48, rgb=(200, 40, 40)) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes(width=width, height=height, rgb=rgb))
    return target


DEFAULT_CONFIG_YAML = """\
operator:
  name: phrase_region_annotation
  version: v1.0.0
stage_id: S3
enabled: false
min_clip_score: 0.25
tokenizer: pretrained
max_tokens: 64
max_text_chars: 500
allow_degraded: false
clip:
  library: auto
  model_name: ViT-B-32
  weights: null
  device: cpu
  batch_size: 8
"""

ENABLED_CONFIG_YAML = DEFAULT_CONFIG_YAML.replace("enabled: false", "enabled: true")


@pytest.fixture()
def config_file(tmp_path) -> Path:
    """默认配置副本（enabled=false，无权重）。"""
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def enabled_config_file(tmp_path) -> Path:
    """开启路线的配置副本（仍无本地权重）。"""
    path = tmp_path / "config_enabled.yaml"
    path.write_text(ENABLED_CONFIG_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def media_dir(tmp_path) -> Path:
    """三张真实 PNG，供图像解析与裁剪使用。"""
    directory = tmp_path / "media"
    write_png(directory / "shot_001.png", rgb=(210, 40, 40))
    write_png(directory / "shot_002.png", rgb=(40, 210, 40))
    write_png(directory / "shot_003.png", rgb=(40, 40, 210))
    return directory


# --------------------------------------------------------------------------- #
# 测试替身编码器
# --------------------------------------------------------------------------- #

def _angle_vector(angle: float) -> List[float]:
    """二维单位向量；两个向量夹角的余弦即为期望分数。"""
    return [math.cos(angle), math.sin(angle)]


class StubEncoder:
    """**测试替身**编码器（不是模型）。

    通过给「词」和「候选框」各指定一个角度，使
    ``cos(text_angle - box_angle)`` 成为可精确预期的余弦分数，
    从而完整覆盖阈值边界、一对一匹配与分流分支。

    参数：
    - ``text_angles``：词 → 角度（弧度）；未列出的词用 ``default_text_angle``；
    - ``box_angles``：``box_id`` → 角度；未列出的框用 ``default_box_angle``；
    - ``crop_errors``：``box_id`` → 错误原因（``media_unreadable`` 等），模拟裁图失败；
    - ``fail``：直接抛异常，模拟编码后端崩溃。
    """

    def __init__(self, text_angles: Optional[Dict[str, float]] = None,
                 box_angles: Optional[Dict[str, float]] = None,
                 crop_errors: Optional[Dict[str, str]] = None,
                 default_text_angle: float = 0.0,
                 default_box_angle: float = math.pi,
                 identity: Optional[Dict[str, Any]] = None,
                 fail: Optional[str] = None) -> None:
        self.text_angles = dict(text_angles or {})
        self.box_angles = dict(box_angles or {})
        self.crop_errors = dict(crop_errors or {})
        self.default_text_angle = default_text_angle
        self.default_box_angle = default_box_angle
        self._identity = identity
        self.fail = fail
        self.text_calls: List[List[str]] = []
        self.crop_calls: List[List[int]] = []
        self.crop_options_calls: List[Optional[Dict[str, Any]]] = []

    # -- 契约接口 ---------------------------------------------------------- #
    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        if self.fail:
            raise RuntimeError(self.fail)
        self.text_calls.append(list(texts))
        return [_angle_vector(self.text_angles.get(text, self.default_text_angle))
                for text in texts]

    def encode_crops(self, image_path, boxes, crop_options=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.crop_calls.append([int(box["bbox"]["x"]) for box in boxes])
        self.crop_options_calls.append(dict(crop_options) if crop_options else None)
        vectors: List[Optional[List[float]]] = []
        errors: List[Optional[str]] = []
        for box in boxes:
            reason = self.crop_errors.get(str(box.get("box_id")))
            if reason:
                vectors.append(None)
                errors.append(reason)
                continue
            angle = self.box_angles.get(str(box.get("box_id")), self.default_box_angle)
            vectors.append(_angle_vector(angle))
            errors.append(None)
        return vectors, errors

    def model_identity(self) -> Dict[str, Any]:
        if self._identity is not None:
            return dict(self._identity)
        return {
            "name": "CLIP-ViT/B-32",
            "arch": "ViT-B-32",
            "version": "test-double",
            "library": "stub",
            "weights": None,
            "device": "cpu",
            "degraded": False,
            "real_backend": False,
            "test_double": True,
        }


def box(box_id: str, x: int = 2, y: int = 2, w: int = 20, h: int = 16,
        label: Optional[str] = None, confidence: Optional[float] = None) -> Dict[str, Any]:
    """构造一条规范候选框（``xywh-pixel``，与 demo 共享 schema 一致）。"""
    payload: Dict[str, Any] = {"box_id": box_id,
                               "bbox": {"x": x, "y": y, "w": w, "h": h,
                                        "coord": "xywh-pixel"}}
    if label is not None:
        payload["class_name"] = label
    if confidence is not None:
        payload["confidence"] = confidence
    return payload


def record(sample_id: str = "sample-0001", text: str = "金毛犬在草地上接飞盘",
           image: str = "media/shot_001.png",
           regions: Optional[List[Dict[str, Any]]] = None,
           tokens: Optional[List[str]] = None,
           **extra: Any) -> Dict[str, Any]:
    """构造一条扁平输入记录（text 对象 / image 字符串 / regions 候选框）。"""
    payload: Dict[str, Any] = {"sample_id": sample_id}
    if text is not None:
        text_obj: Dict[str, Any] = {"content": text, "lang": "zh"}
        if tokens is not None:
            text_obj["tokens"] = list(tokens)
        payload["text"] = text_obj
    if image is not None:
        payload["image"] = image
    if regions is not None:
        payload["regions"] = list(regions)
    payload.update(extra)
    return payload


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
