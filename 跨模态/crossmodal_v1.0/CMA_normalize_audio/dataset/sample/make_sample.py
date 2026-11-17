#!/usr/bin/env python3
# [v4.2] 血缘输出已停用：本脚本生成的 lineage_history.json 仅为历史样例 fixture，
# 算子运行时不再读取/继承血缘；文件保留以维持样例目录结构不变。
"""生成本目录自用的样例 fixture（真实可解码媒体，不依赖任何上游算子）。

产物（相对 ``dataset/sample/``）：

- ``media/aud_sine_22050_stereo_1s.wav``  1.0 秒 / 22050Hz / 双声道 / PCM16（重采样用例）
- ``media/aud_sine_16000_mono_2s.wav``    2.0 秒 / 16000Hz / 单声道 / PCM16（裁剪用例）
- ``media/aud_sine_8000_mono_05s.wav``    0.5 秒 / 8000Hz / 单声道 / PCM16（升采样）
- ``media/aud_8bit_mono_05s.wav``         0.5 秒 / 8000Hz / 单声道 / PCM8（位宽量化）
- ``media/aud_empty.wav``                 合法头、0 帧（空音频失败路径）
- ``media/aud_broken.wav``                非 WAV 字节（损坏 + 依赖缺失路径）
- ``media/img_main.jpg``、``media/vid_main.mp4``：图文/视频引用搬运
- ``demo.jsonl``（8 条完整样本）、``demo_bad.jsonl``（坏行/重复/缺字段）
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


def _sine(rate: int, seconds: float, frequency: float, amplitude: float = 0.4) -> np.ndarray:
    """固定种子的正弦样板（非静音，用于核对输出 RMS>0）。"""
    count = int(round(rate * seconds))
    index = np.arange(count, dtype=np.float64)
    return (amplitude * np.sin(2 * math.pi * frequency * index / rate)).astype(np.float32)


def _pcm16(samples: np.ndarray) -> bytes:
    ints = np.clip(np.rint(samples * 32768.0), -32768, 32767).astype("<i2")
    return ints.tobytes()


def _write_wav_mono(relative_name: str, samples: np.ndarray, rate: int,
                    sampwidth: int = 2) -> Path:
    target = MEDIA_DIR / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if sampwidth == 2:
        payload = _pcm16(samples)
    elif sampwidth == 1:
        payload = np.clip(np.rint(samples * 128.0) + 128.0, 0, 255).astype(np.uint8).tobytes()
    else:
        raise ValueError("样例只生成 8/16 位 WAV")
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(sampwidth)
        writer.setframerate(rate)
        writer.writeframes(payload)
    return target


def _write_wav_stereo(relative_name: str, left: np.ndarray, right: np.ndarray,
                      rate: int) -> Path:
    target = MEDIA_DIR / relative_name
    count = min(left.size, right.size)
    interleaved = np.empty(count * 2, dtype="<i2")
    interleaved[0::2] = np.clip(np.rint(left[:count] * 32768.0), -32768, 32767)
    interleaved[1::2] = np.clip(np.rint(right[:count] * 32768.0), -32768, 32767)
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(interleaved.tobytes())
    return target


def _write_empty_wav(relative_name: str, rate: int = 16000) -> Path:
    """合法 WAV 头但没有采样帧（空音频失败路径）。"""
    target = MEDIA_DIR / relative_name
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"")
    return target


def _write_bytes(relative_name: str, payload: bytes) -> Path:
    target = MEDIA_DIR / relative_name
    target.write_bytes(payload)
    return target


def _write_jpeg(relative_name: str, size=(100, 80)) -> Path:
    target = MEDIA_DIR / relative_name
    height, width = size[1], size[0]
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:, :] = (90, 160, 60)
    frame[20:60, 20:80] = (240, 240, 240)
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise RuntimeError("图片编码失败：%s" % target)
    target.write_bytes(buffer.tobytes())
    return target


def _write_video(relative_name: str, frames: int = 6, fps: float = 10.0,
                 size=(64, 48)) -> Path:
    target = MEDIA_DIR / relative_name
    writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                             (size[0], size[1]))
    if not writer.isOpened():
        raise RuntimeError("无法创建视频写入器：%s" % target)
    height, width = size[1], size[0]
    for index in range(frames):
        frame = np.zeros((height, width, 3), np.uint8)
        frame[:, :] = (60, 120, 200)
        x = 4 + (index * 5) % max(1, width - 16)
        frame[10:30, x:x + 12] = (30, 220, 240)
        writer.write(frame)
    writer.release()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("视频写入失败：%s" % target)
    return target


def _record(sample_id: str, audio=None, image=None, video=None, text=None) -> dict:
    """按 DATA_SCHEMA §7 的完整样本结构造一条记录（模拟 05 的输出）。"""
    return {
        "sample_id": sample_id,
        "text": text if text is not None else {
            "content": "示例语音文本 %s" % sample_id,
            "original": "示例语音文本 %s" % sample_id,
            "tokens": ["示例", "语音", "文本"],
            "token_offsets": [[0, 2], [2, 4], [4, 6]],
            "lang": "zh",
        },
        "image": {
            "path": image, "original_path": image,
            "width": 100, "height": 80, "format": "JPEG", "blur_score": 210.5,
        } if image else None,
        "video": {
            "path": video, "duration": 0.6, "fps": 10.0,
            "frames": [{"path": None, "timestamp": 0.0, "frame_index": 0,
                        "width": 64, "height": 48}],
        } if video else None,
        "audio": {
            "path": audio, "duration": None, "sample_rate": None, "channels": None,
            "segments": [],
        } if audio else None,
        "processing": {
            "02": {"status": "success", "reason": None, "metrics": {}},
            "04": {"status": "success", "reason": None,
                   "metrics": {"images": 1 if image else 0}},
            "05": {"status": "success", "reason": None,
                   "metrics": {"frames_saved": 1 if video else 0}},
        },
        "alignment": {
            "whole": None,
            "local": [],
            "audio_video_pair": (
                [{"audio_sample_id": sample_id, "video_sample_id": sample_id,
                  "similarity": 0.81, "time_range": [0.0, 0.6]}] if (audio and video) else []
            ),
        },
        "qc": {},
        "metadata": {
            "source": "demo_fixture", "license": "internal-demo",
            "collected_at": "2026-10-02T10:00:00+08:00",
        },
    }


def build_records() -> list:
    return [
        # 22050Hz 双声道 -> 16000Hz 单声道（重采样 + 声道平均）
        _record("as_001", audio="media/aud_sine_22050_stereo_1s.wav",
                image="media/img_main.jpg"),
        # 16000Hz 单声道，默认配置下无需重采样（裁剪用例靠 --override 触发）
        _record("as_002", audio="media/aud_sine_16000_mono_2s.wav"),
        # 8000Hz -> 16000Hz 升采样
        _record("as_003", audio="media/aud_sine_8000_mono_05s.wav",
                image="media/img_main.jpg"),
        # 仅图文：本算子 not_applicable，完整透传不生成假 audio
        _record("as_004", image="media/img_main.jpg"),
        # 位宽 8bit -> 16bit 量化
        _record("as_008", audio="media/aud_8bit_mono_05s.wav"),
        # 音频文件缺失：进入 06_rejects.jsonl
        _record("as_005", audio="media/missing_audio.wav", image="media/img_main.jpg"),
        # 音频文件损坏（非法 WAV）：wave 打不开 → ffmpeg 回退 → 依赖缺失
        _record("as_006", audio="media/aud_broken.wav", video="media/vid_main.mp4"),
        # 空音频：合法头、0 帧
        _record("as_007", audio="media/aud_empty.wav"),
    ]


def build_bad_lines() -> str:
    """故意构造坏行/重复/缺字段，用于异常处理用例。"""
    lines = [
        json.dumps({"sample_id": "as_dup", "audio": "media/aud_sine_8000_mono_05s.wav"},
                   ensure_ascii=False),
        json.dumps({"sample_id": "as_dup", "audio": "media/aud_sine_8000_mono_05s.wav"},
                   ensure_ascii=False),
        "{不是合法 JSON",
        json.dumps(["数组不是对象"], ensure_ascii=False),
        json.dumps({"text": {"content": "缺少 sample_id"}}, ensure_ascii=False),
    ]
    return "\n".join(lines) + "\n"


def build_lineage_history() -> list:
    """上一阶段（S1）已归档血缘，随包回传由本算子承接。"""
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
            "config": {"business": {"max_select_per_source": 8}},
            "statistics": {"input": 8, "output": 8, "rejected": 0, "not_applicable": 0},
            "artifacts": [{"path": "01_raw_index.jsonl", "role": "main"}],
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
            "statistics": {"input": 8, "output": 8, "rejected": 0, "not_applicable": 1},
            "artifacts": [{"path": "03_text_cleaned.jsonl", "role": "main"}],
        },
    ]


def build_manifest() -> dict:
    artifacts = [
        {"path": MAIN_ENTRY, "role": "main"},
        {"path": BAD_ENTRY, "role": "review"},
        {"path": LINEAGE_HISTORY, "role": "sidecar"},
    ]
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
    created["aud_sine_22050_stereo_1s.wav"] = _write_wav_stereo(
        "aud_sine_22050_stereo_1s.wav",
        _sine(22050, 1.0, 440.0), _sine(22050, 1.0, 660.0), 22050)
    created["aud_sine_16000_mono_2s.wav"] = _write_wav_mono(
        "aud_sine_16000_mono_2s.wav", _sine(16000, 2.0, 440.0), 16000)
    created["aud_sine_8000_mono_05s.wav"] = _write_wav_mono(
        "aud_sine_8000_mono_05s.wav", _sine(8000, 0.5, 300.0), 8000)
    created["aud_8bit_mono_05s.wav"] = _write_wav_mono(
        "aud_8bit_mono_05s.wav", _sine(8000, 0.5, 300.0), 8000, sampwidth=1)
    created["aud_empty.wav"] = _write_empty_wav("aud_empty.wav", 16000)
    created["aud_broken.wav"] = _write_bytes(
        "aud_broken.wav", b"not a real riff wave payload\n" * 16)
    created["img_main.jpg"] = _write_jpeg("img_main.jpg")
    created["vid_main.mp4"] = _write_video("vid_main.mp4")

    records = build_records()
    _write_text(SAMPLE_DIR / MAIN_ENTRY,
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n")
    _write_text(SAMPLE_DIR / BAD_ENTRY, build_bad_lines())
    _write_text(SAMPLE_DIR / LINEAGE_HISTORY,
                json.dumps(build_lineage_history(), ensure_ascii=False, indent=2) + "\n")
    _write_text(SAMPLE_DIR / "manifest.json",
                json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n")
    return {"records": len(records), "media": len(created)}


def main() -> int:
    summary = write_all()
    print(json.dumps({"sample_dir": str(SAMPLE_DIR), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
