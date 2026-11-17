"""测试用可重复数据构造工具（不依赖网络、不依赖真实模型权重）。

所有图片/音频都由 Python 标准库现场编码为**真实可解码**文件，
因此「不可解码 → 原始隔离」这类分支可以用真实数据驱动。
检测后端用 ``StubDetector`` 注入（测试替身，不代表模型精度）。
"""
from __future__ import annotations

import io
import json
import struct
import wave
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# --------------------------------------------------------------------------- #
# 媒体编码
# --------------------------------------------------------------------------- #

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int = 16, height: int = 16,
              color: tuple = (32, 160, 96)) -> bytes:
    """用 zlib+struct 现场编码一张真实可解码的 RGB PNG。"""

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    r, g, b = color
    raw = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))
    return (PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def wav_bytes(duration_sec: float = 0.2, framerate: int = 16000) -> bytes:
    """标准库生成的 16 位单声道 PCM WAV（用于媒体搬运与引用校验）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = int(duration_sec * framerate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 记录构造
# --------------------------------------------------------------------------- #

def transcript_payload(segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Whisper 风格转写 sidecar：``segments`` 带起止时间。"""
    if segments is None:
        segments = [
            {"id": "seg-0", "start": 0.0, "end": 1.2, "text": "设备开始运转"},
            {"id": "seg-1", "start": 1.2, "end": 2.6, "text": "操作员检查仪表"},
        ]
    return {"language": "zh", "segments": segments}


def make_record(sample_id: Optional[str] = "s-1", *,
                modality: Optional[List[str]] = None,
                description: Optional[str] = "画面中有一名操作员在检查设备",
                image: Optional[str] = None,
                images: Optional[List[str]] = None,
                video: Optional[str] = None,
                audio: Optional[str] = None,
                transcript: Optional[Dict[str, Any]] = None,
                transcript_file: Optional[str] = None,
                human: Optional[Dict[str, Any]] = None,
                alignment: Optional[Dict[str, Any]] = None,
                **extra: Any) -> Dict[str, Any]:
    """构造一条样例记录。

    ``human`` 为人工确认证据（``human_annotations``）；``transcript_file`` 为转写 sidecar。
    """
    record: Dict[str, Any] = {}
    if sample_id is not None:
        record["sample_id"] = sample_id
    record["modality"] = list(modality) if modality is not None else ["image", "text"]
    if description is not None:
        record["text"] = {"content": description}
    if image is not None:
        record["image"] = {"path": image}
    if images is not None:
        record["images"] = [{"path": item} for item in images]
    if video is not None:
        record["video"] = {"path": video}
    if audio is not None:
        record["audio"] = {"path": audio}
    if transcript is not None:
        record["transcript"] = transcript
    if transcript_file is not None:
        record["transcript_file"] = transcript_file
    if human is not None:
        record["human_annotations"] = human
    if alignment is not None:
        record["alignment"] = alignment
    record.update(extra)
    return record


def human_box(identifier: str = "b1", label: str = "person",
              bbox: Optional[List[float]] = None) -> Dict[str, Any]:
    return {"id": identifier, "label": label,
            "bbox": list(bbox) if bbox else [0.10, 0.20, 0.40, 0.75]}


def human_range(identifier: str = "r1", start: float = 0.0,
                end: float = 1.2) -> Dict[str, Any]:
    return {"id": identifier, "start_sec": start, "end_sec": end}


def human_evidence(boxes: Optional[List[Dict[str, Any]]] = None,
                   time_ranges: Optional[List[Dict[str, Any]]] = None, *,
                   verified: bool = True, complete: bool = True,
                   annotator: Optional[str] = "annotator-01") -> Dict[str, Any]:
    payload: Dict[str, Any] = {"human_verified": verified, "complete": complete}
    if annotator is not None:
        payload["annotator"] = annotator
    if boxes is not None:
        payload["boxes"] = boxes
    if time_ranges is not None:
        payload["time_ranges"] = time_ranges
    return payload


# --------------------------------------------------------------------------- #
# 检测替身（测试替身，不代表模型精度）
# --------------------------------------------------------------------------- #

#: 默认检测结果：person + car（car 故意与描述对不上，用于验证过滤）
DEFAULT_DETECTIONS: List[Dict[str, Any]] = [
    {"class_id": 0, "class_name": "person", "confidence": 0.91,
     "x_min": 0.10, "y_min": 0.20, "x_max": 0.40, "y_max": 0.80},
    {"class_id": 2, "class_name": "car", "confidence": 0.77,
     "x_min": 0.55, "y_min": 0.40, "x_max": 0.90, "y_max": 0.70},
]


class StubDetector:
    """测试替身：按图片基线名返回预置框，返回结构必须与真实后端一致。"""

    def __init__(self, detections: Optional[List[Dict[str, Any]]] = None,
                 per_image: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 fail_on: Optional[Iterable[str]] = None) -> None:
        self.detections = list(detections if detections is not None else DEFAULT_DETECTIONS)
        self.per_image = dict(per_image or {})
        self.fail_on = set(fail_on or [])
        self.calls: List[str] = []

    def detect(self, path, *, conf_threshold: float = 0.25, iou_threshold: float = 0.45,
               max_boxes: int = 50, image_size: int = 640) -> List[Dict[str, Any]]:
        name = Path(str(path)).name
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError("stub detector failure: %s" % name)
        items = self.per_image.get(name, self.detections)
        kept = [dict(item) for item in items if item["confidence"] >= conf_threshold]
        return kept[:max_boxes]

    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": "YOLOv8n",
            "library": "stub",
            "version": "test-double",
            "weights": None,
            "weights_sha256": None,
            "device": "cpu",
            "class_source": "coco80",
            "general_classes_only": True,
            "degraded": False,
            "real_backend": False,
            "test_double": True,
        }


# --------------------------------------------------------------------------- #
# 任务与素材包
# --------------------------------------------------------------------------- #

def write_jsonl(path, records: Iterable[Any], *, raw_tail: Optional[str] = None,
                trailing_blank: bool = False) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(record, ensure_ascii=False) if isinstance(record, dict) else str(record)
            for record in records]
    if raw_tail is not None:
        body.append(raw_tail)
    text = "\n".join(body) + "\n"
    if trailing_blank:
        text += "\n"
    target.write_text(text, encoding="utf-8")
    return target


def read_jsonl(path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_task(root, unique_id: str, records: Iterable[Any], *,
               media: Optional[Dict[str, bytes]] = None,
               entrypoint: str = "demo.jsonl",
               manifest: bool = True,
               raw_tail: Optional[str] = None) -> Path:
    """在 ``root/<unique_id>/input`` 下写出一套任务素材，返回入口路径。"""
    task = Path(root) / unique_id
    input_dir = task / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    entry = write_jsonl(input_dir / entrypoint, records, raw_tail=raw_tail)
    for relative, payload in (media or {}).items():
        target = input_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    if manifest:
        (input_dir / "manifest.json").write_text(json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": entrypoint,
            "artifacts": [{"path": entrypoint, "role": "main"}],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def zip_bytes(members: Dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def default_media() -> Dict[str, bytes]:
    return {
        "media/demo.png": png_bytes(),
        "media/absent.png": png_bytes(),
        "media/transcript.json": json.dumps(transcript_payload(),
                                            ensure_ascii=False).encode("utf-8"),
    }


def bundle_zip(records: Optional[Iterable[Dict[str, Any]]] = None,
               media: Optional[Dict[str, bytes]] = None) -> bytes:
    """标准素材包：demo.jsonl + manifest.json + media/*（真实可解码 PNG）。"""
    if records is None:
        records = [
            make_record("api-1", image="media/demo.png",
                        human=human_evidence(boxes=[human_box()])),
            make_record("api-2", image="media/demo.png"),
            make_record("api-3", modality=["text"], description="只有文字的样本"),
        ]
    members: Dict[str, bytes] = {
        "demo.jsonl": ("\n".join(json.dumps(record, ensure_ascii=False)
                                 for record in records) + "\n").encode("utf-8"),
        "manifest.json": json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": "demo.jsonl",
            "artifacts": [
                {"path": "demo.jsonl", "role": "main"},
                {"path": "media/demo.png", "role": "media"},
                {"path": "media/transcript.json", "role": "media"},
            ],
        }, ensure_ascii=False, indent=2).encode("utf-8"),
    }
    members.update(media if media is not None else default_media())
    return zip_bytes(members)
