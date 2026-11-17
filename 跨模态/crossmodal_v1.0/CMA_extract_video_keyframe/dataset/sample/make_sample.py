#!/usr/bin/env python3
# [v4.2] 血缘输出已停用：本脚本生成的 lineage_history.json 仅为历史样例 fixture，
# 算子运行时不再读取/继承血缘；文件保留以维持样例目录结构不变。
"""生成本目录自用的样例 fixture（真实媒体，不依赖任何上游算子）。

产物：
- ``media/*.mp4``：真实可解码视频（OpenCV 写入，固定帧率）
  - ``vid_cfr_2s.mp4``   100x80 / 10fps / 20 帧（2.0 秒，标准 CFR）
  - ``vid_border_1s.mp4`` 带一致近黑外缘（用于裁剪路径）
  - ``vid_short_04s.mp4`` 4 帧 / 0.4 秒（短片边界）
  - ``broken.mp4``       非视频字节（用于不可解码路径）
- ``media/img_main.jpg``、``media/aud_main.wav``：图文/音频引用搬运
- ``demo.jsonl``（5 条完整样本）、``demo_bad.jsonl``（坏行/重复/缺字段）
- ``manifest.json``、``lineage_history.json``

用法::

    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import json
import math
import struct
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"

MAIN_ENTRY = "demo.jsonl"
BAD_ENTRY = "demo_bad.jsonl"
LINEAGE_HISTORY = "lineage_history.json"


def _write_video(relative_name: str, frames: int, fps: float, size=(100, 80),
                 border: int = 0) -> Path:
    """写真实 mp4；``border`` 为四边一致近黑外缘像素宽度。"""
    target = MEDIA_DIR / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                             (size[0], size[1]))
    if not writer.isOpened():
        raise RuntimeError("无法创建视频写入器：%s" % target)
    height, width = size[1], size[0]
    for index in range(frames):
        frame = np.zeros((height, width, 3), np.uint8)
        frame[:, :] = (60, 120, 200)
        # 画一个随时间移动的方块，保证帧内容真实不同
        x = 10 + (index * 7) % max(1, width - 40)
        y = 10 + (index * 5) % max(1, height - 40)
        frame[y:y + 20, x:x + 20] = (30, 220, 240)
        if border:
            frame[:border, :] = 0
            frame[-border:, :] = 0
            frame[:, :border] = 0
            frame[:, -border:] = 0
        writer.write(frame)
    writer.release()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("视频写入失败：%s" % target)
    return target


def _write_jpeg(relative_name: str, size=(100, 80)) -> Path:
    target = MEDIA_DIR / relative_name
    height, width = size[1], size[0]
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:, :] = (90, 160, 60)
    frame[20:60, 20:80] = (240, 240, 240)
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise RuntimeError("图片编码失败")
    target.write_bytes(buffer.tobytes())
    return target


def _write_wav(relative_name: str, seconds: float = 0.5, rate: int = 8000) -> Path:
    target = MEDIA_DIR / relative_name
    frames = int(seconds * rate)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        payload = bytearray()
        for index in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 440 * index / rate))
            payload += struct.pack("<h", value)
        handle.writeframes(bytes(payload))
    return target


def _write_bytes(relative_name: str, payload: bytes) -> Path:
    target = MEDIA_DIR / relative_name
    target.write_bytes(payload)
    return target


def _record(sample_id: str, video: str, image=None, audio=None, text=None) -> dict:
    """按 DATA_SCHEMA 的完整样本结构造一条记录（模拟 04 的输出）。"""
    record = {
        "sample_id": sample_id,
        "text": text if text is not None else {
            "content": "示例文本 %s" % sample_id,
            "lang": "zh",
            "char_count": 10,
            "tokens": 8,
        },
        "image": {"path": image, "width": 100, "height": 80, "format": "JPEG",
                  "blur_score": 210.5, "original_path": image} if image else None,
        "video": video,
        "audio": {"path": audio, "duration": 0.5, "sample_rate": 8000} if audio else None,
        "alignment": {
            "audio_text_pair": [{"sample_id": sample_id, "similarity": 0.93}],
            "audio_video_pair": [{"audio_sample_id": sample_id, "video_sample_id": sample_id,
                                  "similarity": 0.81}] if audio and video else [],
            "image_text_pair": [{"image_sample_id": sample_id, "similarity": 0.88}] if image else [],
        },
        "processing": {
            "01": {"status": "success", "reason": None, "metrics": {}},
            "02": {"status": "success", "reason": None, "metrics": {}},
            "03": {"status": "success", "reason": None,
                   "metrics": {"text_changed": 1, "bad_lines": 0}},
            "04": {"status": "success", "reason": None, "metrics": {"images": 1 if image else 0}},
        },
        "qc": {"asr_confidence": 0.91, "ocr_confidence": 0.87, "blur_score": 210.5,
               "duplication_flag": False, "mismatch_flag": False},
        "metadata": {"source": "demo_fixture", "license": "internal-demo",
                     "collected_at": "2026-10-02T10:00:00+08:00"},
    }
    return record


def build_records() -> list:
    return [
        _record("vk_001", "media/vid_cfr_2s.mp4", image="media/img_main.jpg",
                audio="media/aud_main.wav"),
        _record("vk_002", "media/vid_border_1s.mp4", image="media/img_main.jpg"),
        _record("vk_003", "media/vid_short_04s.mp4"),
        # 无视频：本算子 not_applicable，记录原样保留
        _record("vk_004", None, image="media/img_main.jpg"),
        # 视频文件缺失：进入 05_rejects.jsonl
        _record("vk_005", "media/missing_video.mp4"),
        # 视频文件不是视频：解码失败
        _record("vk_006", "media/broken.mp4"),
    ]


def build_bad_lines() -> str:
    """故意构造坏行/重复/缺字段，用于异常处理用例。"""
    lines = [
        json.dumps({"sample_id": "vk_dup", "video": "media/vid_cfr_2s.mp4"}, ensure_ascii=False),
        json.dumps({"sample_id": "vk_dup", "video": "media/vid_cfr_2s.mp4"}, ensure_ascii=False),
        "{不是合法 JSON",
        json.dumps(["数组不是对象"], ensure_ascii=False),
        json.dumps({"text": {"content": "缺少 sample_id"}}, ensure_ascii=False),
    ]
    return "\n".join(lines) + "\n"


def build_lineage_history() -> list:
    return [
        {
            "operator": "data_selection",
            "operator_version": "v1.0.0",
            "uniqueId": "demoS2_f1",
            "stageId": "S1",
            "runId": "run001",
            "status": "success",
            "startedAt": "2026-10-02T09:00:00+08:00",
            "finishedAt": "2026-10-02T09:01:00+08:00",
            "config": {"deployment": {"operator": {"name": "data_selection", "version": "v1.0.0"}},
                       "business": {"max_select_per_source": 1}},
            "statistics": {"input": 6, "output": 6, "rejected": 0, "not_applicable": 0},
            "artifacts": [{"path": "01_raw_index.jsonl", "role": "main"}],
        },
        {
            "operator": "image_text_selection",
            "operator_version": "v1.0.0",
            "uniqueId": "demoS2_f1",
            "stageId": "S1",
            "runId": "run002",
            "status": "success",
            "startedAt": "2026-10-02T09:10:00+08:00",
            "finishedAt": "2026-10-02T09:12:00+08:00",
            "config": {"business": {"text_image_ratio": [1, 3]}},
            "statistics": {"input": 6, "output": 6, "rejected": 0, "not_applicable": 1},
            "artifacts": [{"path": "02_image_index.jsonl", "role": "main"}],
        },
        {
            "operator": "text_cleaning",
            "operator_version": "v1.0.0",
            "uniqueId": "demoS2_f1",
            "stageId": "S2",
            "runId": "run001",
            "status": "success",
            "startedAt": "2026-10-02T09:20:00+08:00",
            "finishedAt": "2026-10-02T09:21:00+08:00",
            "config": {"business": {"normalize_unicode": True}},
            "statistics": {"input": 6, "output": 6, "rejected": 0, "not_applicable": 0},
            "artifacts": [{"path": "03_text_cleaned.jsonl", "role": "main"}],
        },
    ]


def build_manifest(records: list) -> dict:
    artifacts = [{"path": MAIN_ENTRY, "role": "main"},
                 {"path": BAD_ENTRY, "role": "review"},
                 {"path": LINEAGE_HISTORY, "role": "sidecar"}]
    for path in sorted(MEDIA_DIR.iterdir()):
        if path.is_file():
            artifacts.append({"path": "media/%s" % path.name, "role": "media"})
    return {"format": "crossmodal-manifest-v1", "entrypoint": MAIN_ENTRY,
            "artifacts": artifacts}


def _write_text(path: Path, text: str) -> None:
    """显式 LF 写入（Python 3.9 的 Path.write_text 没有 newline 参数）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def write_all() -> dict:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    created = {}
    created["vid_cfr_2s.mp4"] = _write_video("vid_cfr_2s.mp4", frames=20, fps=10.0)
    created["vid_border_1s.mp4"] = _write_video("vid_border_1s.mp4", frames=10, fps=10.0,
                                               border=8)
    created["vid_short_04s.mp4"] = _write_video("vid_short_04s.mp4", frames=4, fps=10.0)
    created["img_main.jpg"] = _write_jpeg("img_main.jpg")
    created["aud_main.wav"] = _write_wav("aud_main.wav")
    created["broken.mp4"] = _write_bytes("broken.mp4", b"not a real video container\n" * 8)

    records = build_records()
    _write_text(SAMPLE_DIR / MAIN_ENTRY,
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n")
    _write_text(SAMPLE_DIR / BAD_ENTRY, build_bad_lines())
    _write_text(SAMPLE_DIR / LINEAGE_HISTORY,
                json.dumps(build_lineage_history(), ensure_ascii=False, indent=2) + "\n")
    _write_text(SAMPLE_DIR / "manifest.json",
                json.dumps(build_manifest(records), ensure_ascii=False, indent=2) + "\n")
    return {"records": len(records), "media": len(created)}


def main() -> int:
    summary = write_all()
    print(json.dumps({"sample_dir": str(SAMPLE_DIR), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
