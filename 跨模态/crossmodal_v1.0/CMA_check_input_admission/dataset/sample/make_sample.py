"""生成算子 10 的可重复样例：``demo.jsonl`` + ``media/`` + ``manifest.json``。

- 全部素材在本地真实生成（PNG/WAV 纯 Python，MP4 优先 OpenCV），**不下载任何外部数据集**；
- 覆盖表1 #10 的全部分流：4 条合格、3 条退回（#9 / 批次登记）、3 条原始隔离、
  4 条样本隔离（含 1 条退回超限锁定），另有 1 行坏 JSON 与 1 行非对象行；
- 重新运行结果稳定（无随机数、无时间戳），可直接用于回归。

用法：``python dataset/sample/make_sample.py``
"""
from __future__ import annotations

import io
import json
import math
import struct
import sys
import wave
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
ENTRYPOINT = "demo.jsonl"

TRAINABLE = "可训练"


# ------------------------------------------------------------------ 媒体生成
def png_bytes(width: int = 64, height: int = 64, rgb=(200, 40, 40)) -> bytes:
    import zlib

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def wav_bytes(seconds: float = 0.15, rate: int = 16000, freq: float = 440.0) -> bytes:
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
    """最小 ``ftyp`` box：头签名合法但不可播放（仅 header 模式能通过）。"""
    return struct.pack(">I", 16) + b"ftyp" + b"isom" + struct.pack(">I", 0)


def write_mp4(path: Path) -> bool:
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
    except Exception:  # noqa: BLE001
        path.write_bytes(minimal_ftyp_mp4())
        return False


def build_media() -> dict:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / "img_ok.png").write_bytes(png_bytes())
    (MEDIA_DIR / "img_broken.png").write_bytes(b"this-is-not-a-real-png\x00\x01\x02" * 4)
    (MEDIA_DIR / "img_empty.png").write_bytes(b"")
    (MEDIA_DIR / "audio_ok.wav").write_bytes(wav_bytes())
    real_video = write_mp4(MEDIA_DIR / "video_ok.mp4")
    return {"video_decodable": real_video}


# ------------------------------------------------------------------ 记录生成
def record(sample_id, *, text="红色方块在白色桌面上", description_source="图注",
           image="media/img_ok.png", video=None, audio=None,
           source_id="SRC-A", batch_id="BATCH-2026-10", license_value=TRAINABLE,
           retry_count=0, image_tags=None, description_in_metadata=None) -> dict:
    item = {
        "sample_id": sample_id,
        "text": None if text is None else {
            "content": text, "original": text, "tokens": ["红色", "方块"], "lang": "zh",
            "description_source": description_source,
        },
        "image": None if image is None else {"path": image, "width": 64, "height": 64,
                                             "format": "PNG"},
        "video": None if video is None else {"path": video, "format": "MP4"},
        "audio": None if audio is None else {"path": audio, "sample_rate": 16000,
                                             "channels": 1},
        "semantic_unit": "%s:unit:0001" % sample_id,
        "alignment": {"whole": None, "local": []},
        "qc": {},
        "metadata": {"image_tags": list(image_tags if image_tags is not None else ["红色", "方块"])},
        "processing": {"09": {"status": "success", "description_source": description_source,
                              "metrics": {"chars": len(text or "")}}},
    }
    if source_id is not None:
        item["metadata"]["source_id"] = source_id
    if batch_id is not None:
        item["metadata"]["batch_id"] = batch_id
    if license_value is not None:
        item["metadata"]["license"] = license_value
    if retry_count:
        item["metadata"]["retry_count"] = retry_count
    if description_in_metadata is not None:
        # 音视频样本的描述来自字幕/语音转写，统一放在 metadata.description
        item["metadata"]["description"] = description_in_metadata
        item["metadata"]["description_source"] = description_source
    return item


def build_records() -> list:
    describe = "两个人先握手再一起走向仓库货架"
    return [
        # 1–4 合格待配对
        record("s-ok-1"),
        record("s-ok-2", image="media/img_ok.png", source_id="SRC-B"),
        record("s-ok-3", text=None, image=None, video="media/video_ok.mp4",
               audio="media/audio_ok.wav", source_id="SRC-C", description_source="语音转写",
               description_in_metadata=describe),
        record("s-ok-4", text="红色方块旁边放着一段提示音", audio="media/audio_ok.wav",
               source_id="SRC-D"),
        # 5–7 退回 #9 / 批次登记
        record("s-ret-1", text=""),
        record("s-ret-2", text="img_ok"),
        record("s-ret-3", batch_id=None),
        # 8–10 原始隔离区
        record("s-orq-1", image="media/nonexistent.png"),
        record("s-orq-2", image="media/img_broken.png"),
        record("s-orq-3", image="media/img_empty.png"),
        # 11–13 样本隔离库
        record("s-sq-1", license_value="不可训练"),
        record("s-sq-2", image=None),
        record("s-sq-3", text=None, image="media/img_ok.png", video="media/video_ok.mp4",
               license_value="待确认", description_source="人工补写",
               description_in_metadata="货车驶入月台后开始卸货"),
        # 16 退回超限 → 锁定样本隔离库
        record("s-sq-4", text="", retry_count=2),
    ]


def build_lines() -> list:
    lines = [json.dumps(item, ensure_ascii=False) for item in build_records()]
    # 坏行：JSON 解析失败 + 顶层不是对象（都必须进原始隔离台账，不能静默丢弃）
    lines.append('{"sample_id": "s-bad-json", "text": ')
    lines.append('[{"sample_id": "s-bad-shape"}]')
    return lines


def main() -> int:
    media_info = build_media()
    lines = build_lines()
    entry = SAMPLE_DIR / ENTRYPOINT
    entry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    artifacts = [{"path": ENTRYPOINT, "role": "main"}]
    for path in sorted(MEDIA_DIR.iterdir()):
        if path.is_file():
            artifacts.append({"path": "media/%s" % path.name, "role": "media"})
    manifest = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": ENTRYPOINT,
        "artifacts": artifacts,
        "operator": "input_admission",
        "formal_operator_numbers": [10],
        "notes": "算子10 表1 #10 准入核对样例；4 合格 / 3 退回 / 3 原始隔离 / 4 样本隔离 / 2 坏行",
    }
    (SAMPLE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    media_count = len([item for item in artifacts if item["role"] == "media"])
    print("demo.jsonl 行数=%d" % len(lines))
    print("media 文件数=%d" % media_count)
    print("video_decodable=%s" % media_info["video_decodable"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
