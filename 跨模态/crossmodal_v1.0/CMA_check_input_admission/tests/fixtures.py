"""算子 10 测试夹具：真实生成最小可用媒体与跨模态记录。

约定：
- 媒体全部在测试时用标准库/OpenCV 真实生成（PNG/WAV 为纯 Python 生成），
  不下载任何外部数据集；
- ``build_media`` 同时产出“损坏”“空文件”变体，用于原始隔离区测试；
- 记录结构遵循 ``dataset/DATA_SCHEMA.md``（#9 输出 → #10 输入）。
"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = BASE_DIR / "dataset" / "sample"
SAMPLE_MEDIA = SAMPLE_DIR / "media"
SAMPLE_ENTRYPOINT = "demo.jsonl"

#: 表1 #10 只接受“可训练”授权结论
VALID_LICENSE = "可训练"


# ------------------------------------------------------------------ 媒体生成
def png_bytes(width: int = 64, height: int = 64, rgb=(200, 40, 40)) -> bytes:
    """生成合法 PNG（无第三方依赖）。"""
    import zlib

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def wav_bytes(seconds: float = 0.15, rate: int = 16000, freq: float = 440.0) -> bytes:
    """生成 16bit 单声道 PCM WAV（非静音）。"""
    frames = max(1, int(seconds * rate))
    data = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * index / rate)))
        for index in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)
    return buffer.getvalue()


def minimal_ftyp_mp4() -> bytes:
    """最小 ``ftyp`` box：*头签名*合法，但不是可播放视频（仅 header 模式能通过）。"""
    return struct.pack(">I", 16) + b"ftyp" + b"isom" + struct.pack(">I", 0)


def write_mp4(path: Path) -> bool:
    """优先用 OpenCV 写真实可解码 mp4；不可用时退回最小 ftyp 头。

    返回 True 表示写入了可被 OpenCV 解码的真实视频。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        path.write_bytes(minimal_ftyp_mp4())
        return False
    try:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
        if not writer.isOpened():
            raise RuntimeError("VideoWriter 未打开")
        for index in range(4):
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            frame[:, :] = ((index * 50) % 255, 30, 180)
            writer.write(frame)
        writer.release()
        return path.is_file() and path.stat().st_size > 0
    except Exception:  # noqa: BLE001 - 退回最小头即可满足 header 模式
        path.write_bytes(minimal_ftyp_mp4())
        return False


def build_media(directory: Path) -> Dict[str, Path]:
    """在 ``directory`` 下生成一整套测试媒体（含损坏/空文件变体）。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "image_ok": directory / "img_ok.png",
        "image_broken": directory / "img_broken.png",
        "image_empty": directory / "img_empty.png",
        "audio_ok": directory / "audio_ok.wav",
        "video_ok": directory / "video_ok.mp4",
        "text_ok": directory / "note.txt",
    }
    files["image_ok"].write_bytes(png_bytes())
    files["image_broken"].write_bytes(b"this-is-not-a-real-png\x00\x01\x02" * 4)
    files["image_empty"].write_bytes(b"")
    files["audio_ok"].write_bytes(wav_bytes())
    write_mp4(files["video_ok"])
    files["text_ok"].write_text("外部文本模态占位内容", encoding="utf-8")
    return files


# ------------------------------------------------------------------ 记录构造
def make_record(sample_id: str,
                *,
                text: Optional[str] = "红色方块在白色桌面上",
                description_source: str = "图注",
                image: Optional[str] = "media/img_ok.png",
                video: Optional[str] = None,
                audio: Optional[str] = None,
                source_id: Optional[str] = "SRC-A",
                batch_id: Optional[str] = "BATCH-2026-10",
                license: Optional[str] = VALID_LICENSE,
                retry_count: int = 0,
                image_tags: Optional[Iterable[str]] = None,
                extra_metadata: Optional[Dict[str, Any]] = None,
                extra_record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条 #9 输出 / #10 输入记录。"""
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "text": None if text is None else {
            "content": text,
            "original": text,
            "tokens": ["红色", "方块"],
            "lang": "zh",
            "description_source": description_source,
        },
        "image": None if image is None else {
            "path": image, "width": 64, "height": 64, "format": "PNG"},
        "video": None if video is None else {"path": video},
        "audio": None if audio is None else {"path": audio, "sample_rate": 16000, "channels": 1},
        "semantic_unit": "%s:unit:0001" % sample_id,
        "alignment": {"whole": None, "local": []},
        "qc": {},
        "metadata": {"image_tags": list(image_tags if image_tags is not None else ["红色", "方块"])},
    }
    if source_id is not None:
        record["metadata"]["source_id"] = source_id
    if batch_id is not None:
        record["metadata"]["batch_id"] = batch_id
    if license is not None:
        record["metadata"]["license"] = license
    if retry_count:
        record["metadata"]["retry_count"] = retry_count
    if extra_metadata:
        record["metadata"].update(extra_metadata)
    record["processing"] = {"09": {"status": "success",
                                   "description_source": description_source,
                                   "metrics": {"chars": len(text or "")}}}
    if extra_record:
        record.update(extra_record)
    return record


def write_input_dir(root,
                    records: Optional[Iterable[Dict[str, Any]]] = None,
                    *,
                    entrypoint: str = SAMPLE_ENTRYPOINT,
                    media: Optional[Dict[str, bytes]] = None,
                    manifest: bool = True,
                    raw_lines: Optional[Iterable[str]] = None,
                    entry_bytes: Optional[bytes] = None,
                    with_media: bool = True) -> Path:
    """把记录与媒体落到 ``root``，返回入口文件路径。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    media_dir = root / "media"
    if with_media:
        if media is None:
            build_media(media_dir)
        else:
            media_dir.mkdir(parents=True, exist_ok=True)
            for name, blob in media.items():
                target = media_dir / Path(name).name
                target.write_bytes(blob)

    entry = root / entrypoint
    if entry_bytes is not None:
        entry.write_bytes(entry_bytes)
    else:
        lines: List[str] = []
        if raw_lines is not None:
            lines = list(raw_lines)
        else:
            for record in records or []:
                lines.append(json.dumps(record, ensure_ascii=False))
        entry.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    if manifest:
        payload: Dict[str, Any] = {
            "format": "crossmodal-manifest-v1",
            "entrypoint": entrypoint,
            "artifacts": [{"path": entrypoint, "role": "main"}],
        }
        if with_media:
            for path in sorted(media_dir.rglob("*")):
                if path.is_file():
                    payload["artifacts"].append(
                        {"path": path.relative_to(root).as_posix(), "role": "media"})
        (root / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return entry


def zip_directory(source, destination) -> Path:
    """把目录压成 zip（保留相对路径）。"""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return destination


def make_zip(destination, members: Dict[str, bytes]) -> Path:
    """按 ``{相对路径: 字节}`` 直接构造 zip（用于 zip-slip 等安全测试）。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)
    return destination


def sample_records() -> List[Dict[str, Any]]:
    """返回 dataset/sample/demo.jsonl 的期望分类（与 make_sample.py 保持一致）。"""
    return []


def read_jsonl(path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
