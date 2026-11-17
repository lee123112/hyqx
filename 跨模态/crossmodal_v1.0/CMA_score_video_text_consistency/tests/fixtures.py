"""算子 14 测试样本构造器与测试替身。

- 记录构造器与 #11 ``11_candidate_pairs.jsonl`` 结构一致（``left``/``right`` + ``record``）；
- ``StubEncoder`` 是**明确标注的测试替身**，只用于验证分段/聚合/路由/台账逻辑，
  ``model_identity()`` 显式写 ``test-double``，绝不进入生产路径，也不作为模型精度证据。
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from conftest import png_bytes, write_png

DEFAULT_FRAME_NAMES = ("frames/frame_0001.png", "frames/frame_0002.png",
                       "frames/frame_0003.png")


# --------------------------------------------------------------------------- #
# 记录构造
# --------------------------------------------------------------------------- #

def frame(path: str, timestamp: Optional[float] = None,
          frame_index: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"path": path, "timestamp": timestamp}
    if frame_index is not None:
        payload["frame_index"] = frame_index
    return payload


def video_object(path: str = "video/clip.mp4", *, frames: Optional[Sequence[Dict]] = None,
                 segments: Optional[Sequence[Dict]] = None, duration: float = 4.0,
                 fps: float = 2.0) -> Dict[str, Any]:
    return {
        "path": path,
        "duration": duration,
        "fps": fps,
        "frames": [dict(item) for item in (frames or [])],
        "segments": [dict(item) for item in (segments or [])],
    }


def _pair_shell(pair_id: str, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pair_id": pair_id,
        "group_id": "grp-src-src-A",
        "modality_pair": ["text", "video"],
        "evidence": {"type": "A", "criterion": "same_source_id",
                     "value": {"source_id": "src-A"}},
        "evidence_priority": 1,
        "left": {"sample_id": left["sample_id"], "modalities": ["text", "video"],
                 "record": left},
        "right": {"sample_id": right["sample_id"], "modalities": ["text", "video"],
                  "record": right},
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
                     "uniqueId": "task_file"},
    }


def make_pair(pair_id: str, description: str, frames: Sequence[Dict],
              *, video_path: str = "video/clip.mp4", segments: Optional[Sequence[Dict]] = None,
              video_side: str = "left", duration: float = 4.0,
              left_id: Optional[str] = None, right_id: Optional[str] = None,
              **extra: Any) -> Dict[str, Any]:
    """构造 #11 形态的视文候选配对（一侧视频帧序列，一侧文本描述）。"""
    video_record = {
        "sample_id": left_id or "%s-video" % pair_id,
        "source_id": "src-A",
        "batch_id": "batch-01",
        "video": video_object(video_path, frames=frames, segments=segments, duration=duration),
        "image": None,
        "text": None,
        "processing": {},
        "metadata": {},
    }
    text_record = {
        "sample_id": right_id or "%s-text" % pair_id,
        "source_id": "src-A",
        "batch_id": "batch-01",
        "video": None,
        "image": None,
        "text": {"content": description, "original": description, "lang": "zh"},
        "processing": {},
        "metadata": {"description_source": "caption"},
    }
    left, right = ((video_record, text_record) if video_side == "left"
                   else (text_record, video_record))
    record = _pair_shell(pair_id, left, right)
    record.update(extra)
    return record


def make_flat_record(sample_id: str, description: str, frames: Sequence[Dict],
                     *, video_path: str = "video/clip.mp4",
                     segments: Optional[Sequence[Dict]] = None,
                     duration: float = 4.0, **extra: Any) -> Dict[str, Any]:
    """单记录形态：同一记录内同时含视频帧序列与文本描述。"""
    record = {
        "sample_id": sample_id,
        "source_id": "src-B",
        "batch_id": "batch-01",
        "video": video_object(video_path, frames=frames, segments=segments, duration=duration),
        "image": None,
        "text": {"content": description, "original": description, "lang": "zh"},
        "processing": {},
        "metadata": {"description_source": "caption"},
    }
    record.update(extra)
    return record


def default_frames(count: int = 3, *, prefix: str = "frames/frame",
                   timestamps: bool = True, fps: float = 2.0) -> List[Dict[str, Any]]:
    items = []
    for index in range(1, count + 1):
        items.append(frame("%s_%04d.png" % (prefix, index),
                           (index - 1) / fps if timestamps else None, index))
    return items


# --------------------------------------------------------------------------- #
# 输入目录 / JSONL / zip
# --------------------------------------------------------------------------- #

def write_jsonl(path, records: Iterable[Dict[str, Any]],
                *, raw_lines: Optional[List[str]] = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw_lines is not None:
        target.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        return target
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def build_input_tree(root, records, *, media: Optional[Dict[str, bytes]] = None,
                     entrypoint: str = "demo.jsonl", raw_lines: Optional[List[str]] = None,
                     encoding: str = "utf-8") -> Path:
    """构造一个「输入素材目录」：入口 JSONL + media/。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    if raw_lines is not None:
        payload = "\n".join(raw_lines) + "\n"
    else:
        payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    (base / entrypoint).write_bytes(payload.encode(encoding))
    for name, blob in (media or {}).items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return base


def default_media(names: Sequence[str] = DEFAULT_FRAME_NAMES) -> Dict[str, bytes]:
    palette = [(210, 40, 40), (40, 210, 40), (40, 40, 210), (200, 200, 40)]
    media: Dict[str, bytes] = {}
    for index, name in enumerate(names):
        media[name] = png_bytes(rgb=palette[index % len(palette)])
    return media


def zip_bytes(members: Dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def read_jsonl(path) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def read_lines(path) -> List[str]:
    target = Path(path)
    if not target.is_file():
        return []
    return [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 测试替身编码器
# --------------------------------------------------------------------------- #

class StubEncoder:
    """测试替身：按文本给出确定余弦（与 ``[1, 0]`` 图像向量点积即为该值）。

    ``model_identity()`` 显式声明 ``test-double``，避免任何「模型不可用但仍标 model」
    或「把测试分当成模型精度」的误读。
    """

    name = "CLIP-ViT/B-32"

    def __init__(self, scores: Optional[Dict[str, float]] = None, default: float = 0.9,
                 unreadable: Sequence[str] = (), fail: bool = False) -> None:
        self.scores = dict(scores or {})
        self.default = float(default)
        self.unreadable = set(unreadable)
        self.fail = bool(fail)
        self.text_calls: List[str] = []
        self.image_calls: List[str] = []

    def _score_for(self, text: str) -> float:
        value = self.scores.get(text, self.default)
        return max(-1.0, min(1.0, float(value)))

    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        if self.fail:
            raise RuntimeError("stub encoder failure")
        vectors = []
        for text in texts:
            self.text_calls.append(text)
            value = self._score_for(text)
            vectors.append([value, math.sqrt(max(0.0, 1.0 - value * value))])
        return vectors

    def encode_images(self, paths: Sequence[Optional[Path]]):
        if self.fail:
            raise RuntimeError("stub encoder failure")
        vectors: List[Optional[List[float]]] = []
        errors: List[Optional[str]] = []
        for path in paths:
            if path is None:
                vectors.append(None)
                errors.append("media_missing")
                continue
            target = Path(path)
            self.image_calls.append(target.name)
            if not target.is_file():
                vectors.append(None)
                errors.append("media_missing")
                continue
            if target.name in self.unreadable or not _decodable_image(target):
                # 与真实后端一致：文件存在但无法解码 → media_unreadable（原始文件问题）
                vectors.append(None)
                errors.append("media_unreadable")
                continue
            vectors.append([1.0, 0.0])
            errors.append(None)
        return vectors, errors

    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": "test-double",
            "library": "test-double",
            "weights": None,
            "device": "cpu",
            "degraded": False,
            "real_backend": False,
            "untrained_architecture_only": False,
            "score_semantics": "cosine",
            "test_double": True,
        }


def make_config(**overrides: Any):
    """按表1 默认口径构造 ``OperatorConfig``；``clip`` 子项可整体覆盖。"""
    from utils.schemas import OperatorConfig

    payload: Dict[str, Any] = {
        "operator": {"name": "video_text_scoring", "version": "v1.0.0"},
        "stage_id": "S3",
        "enabled": True,
        "low_score_threshold": 0.25,
        "max_frames_per_segment": 8,
        "max_text_chars": 500,
        "allow_degraded": False,
        "clip": {"library": "auto", "model_name": "ViT-B-32", "weights": None,
                 "device": "cpu", "batch_size": 8},
    }
    clip_overrides = overrides.pop("clip", None)
    payload.update(overrides)
    if clip_overrides:
        payload["clip"] = dict(payload["clip"], **clip_overrides)
    return OperatorConfig.model_validate(payload)


def _decodable_image(path: Path) -> bool:
    """真实尝试解码；测试替身也据此判定 ``media_unreadable``，避免假装成功。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
        return True
    except Exception:  # noqa: BLE001 - 任何解码异常都视为不可读
        return False


def media_with_unreadable(names: Sequence[str] = DEFAULT_FRAME_NAMES,
                          broken: Sequence[str] = ()) -> Dict[str, bytes]:
    """把指定帧替换为不可解码内容；``broken`` 支持相对路径或纯文件名。"""
    media = default_media(names)
    targets = set(broken)
    for key in list(media):
        if key in targets or Path(key).name in targets:
            media[key] = b"not an image"
    return media
