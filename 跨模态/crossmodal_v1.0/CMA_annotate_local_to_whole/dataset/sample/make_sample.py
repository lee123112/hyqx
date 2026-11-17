"""生成可重复的样例数据集（只用 Python 标准库，不联网、不下载任何外部数据）。

输出到本目录：
- ``demo.jsonl``        入口记录（覆盖 confirmed / pending / 样本隔离 / 不适用 / 原始隔离五条路线）
- ``manifest.json``     入口清单
- ``media/demo.png``    真实可解码 PNG（zlib+struct 现场编码）
- ``media/clip.wav``    真实可解码 PCM WAV
- ``media/transcript.json`` 转写 sidecar

用法：
    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import io
import json
import struct
import wave
import zlib
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
DEMO_NAME = "demo.jsonl"

TRANSCRIPT = {
    "language": "zh",
    "model": "whisper-large-v3-demo",
    "segments": [
        {"id": "seg-0", "start": 0.0, "end": 1.4, "text": "操作员打开设备电源"},
        {"id": "seg-1", "start": 1.4, "end": 3.2, "text": "操作员检查仪表读数"},
        {"id": "seg-2", "start": 3.2, "end": 5.0, "text": "设备开始正常运转"},
    ],
}


def png_bytes(width: int = 24, height: int = 24, color=(24, 132, 96)) -> bytes:
    """现场编码一张真实可解码的 RGB PNG（PNG 签名 + IHDR/IDAT/IEND）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def wav_bytes(duration_sec: float = 0.5, framerate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"\x00\x00" * int(duration_sec * framerate))
    return buffer.getvalue()


def demo_records():
    """8 条记录，覆盖表1 #17 的全部路线。"""
    return [
        # 1) 图像 + 描述 + 人工确认框 → confirmed（可交付）
        {
            "sample_id": "demo-img-confirmed",
            "modality": ["image", "text"],
            "text": {"content": "画面中有一名操作员在检查设备仪表"},
            "image": {"path": "media/demo.png"},
            "batch_id": "batch-demo",
            "human_annotations": {
                "human_verified": True,
                "complete": True,
                "annotator": "annotator-demo",
                "boxes": [{"id": "b1", "label": "person",
                           "bbox": [0.12, 0.18, 0.42, 0.76]}],
            },
        },
        # 2) 图像 + 描述，人工尚未确认 → pending_manual_review（不可交付）
        {
            "sample_id": "demo-img-pending",
            "modality": ["image", "text"],
            "text": {"content": "画面中有一名操作员站在设备旁边"},
            "image": {"path": "media/demo.png"},
            "batch_id": "batch-demo",
        },
        # 3) 人工声称已确认但标注不完整 → 样本隔离库（样本-语义不通）
        {
            "sample_id": "demo-img-incomplete",
            "modality": ["image", "text"],
            "text": {"content": "画面中有一名操作员在操作面板"},
            "image": {"path": "media/demo.png"},
            "batch_id": "batch-demo",
            "human_annotations": {"human_verified": True, "complete": False},
        },
        # 4) 音视频：沿用转写起止时间预填，人工未确认 → pending
        {
            "sample_id": "demo-av-pending",
            "modality": ["audio", "text"],
            "text": {"content": "设备运转声音"},
            "audio": {"path": "media/clip.wav"},
            "transcript_file": "media/transcript.json",
            "batch_id": "batch-demo",
        },
        # 5) 音视频：人工确认真实起止时间段 → confirmed
        {
            "sample_id": "demo-av-confirmed",
            "modality": ["audio", "text"],
            "text": {"content": "设备运转声音"},
            "audio": {"path": "media/clip.wav"},
            "transcript_file": "media/transcript.json",
            "batch_id": "batch-demo",
            "human_annotations": {
                "human_verified": True,
                "complete": True,
                "annotator": "annotator-demo",
                "time_ranges": [{"id": "r1", "start_sec": 0.0, "end_sec": 1.4}],
            },
        },
        # 6) 纯文字样本 → not_applicable（留样本池，不隔离）
        {
            "sample_id": "demo-text-only",
            "modality": ["text"],
            "text": {"content": "这条样本只有文字描述，没有图像或音视频"},
            "batch_id": "batch-demo",
        },
        # 7) 人工证据结构非法 → 原始隔离（字段非法）
        {
            "sample_id": "demo-bad-human",
            "modality": ["image", "text"],
            "text": {"content": "画面中有一名操作员"},
            "image": {"path": "media/demo.png"},
            "batch_id": "batch-demo",
            "human_annotations": {"human_verified": True, "boxes": "oops"},
        },
        # 8) 媒体引用缺失 → 原始隔离（缺关键字段）
        {
            "sample_id": "demo-missing-image",
            "modality": ["image", "text"],
            "text": {"content": "画面中有一名操作员"},
            "image": {"path": "media/absent.png"},
            "batch_id": "batch-demo",
        },
    ]


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / "demo.png").write_bytes(png_bytes())
    (MEDIA_DIR / "clip.wav").write_bytes(wav_bytes())
    (MEDIA_DIR / "transcript.json").write_text(
        json.dumps(TRANSCRIPT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    records = demo_records()
    entry = SAMPLE_DIR / DEMO_NAME
    entry.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n"
                             for record in records), encoding="utf-8")

    manifest = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": DEMO_NAME,
        "artifacts": [
            {"path": DEMO_NAME, "role": "main"},
            {"path": "media/demo.png", "role": "media"},
            {"path": "media/clip.wav", "role": "media"},
            {"path": "media/transcript.json", "role": "media"},
        ],
        "note": "跨模态 demo_10.6 算子 #17 样例：media/absent.png 故意缺失，用于演示原始隔离",
    }
    (SAMPLE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("sample written: %s (%d records)" % (entry, len(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
