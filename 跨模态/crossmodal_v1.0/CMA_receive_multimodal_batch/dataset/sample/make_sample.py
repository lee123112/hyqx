"""生成算子 01 的确定性样例：``demo.jsonl`` + 本地媒体文件。

样例**完全自包含**（不下载任何外部数据）：媒体字节用标准库按真实文件头构造，
每次运行结果一致，便于 CLI 冒烟与回归比对。

用法::

    python dataset/sample/make_sample.py

覆盖场景
--------
1. 采集平台批次 + 本地 PNG（``header`` 模式下走本地文件头校验 → local_verified）
2. 预训练图像产线交付 + 远端 OSS 链接（只登记链接 → link_only）
3. 预训练音频产线交付 + wav
4. 预训练视频产线交付 + mp4
5. 预训练文本产线交付 + json
6. 多文件记录（同一 sample_id 的图像 + 音频）
7. 格式不在白名单（gif）→ 原始隔离区
8. 授权不可训练 → 样本隔离库
9. 缺批次号 → 待补登记退回
10. 缺 sample_id → 原始隔离区
11. 重复登记 → 待补登记退回
12. 坏 JSON 行、非对象行 → 原始隔离区（拒绝子类）
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO_PATH = HERE / "demo.jsonl"


def png_bytes(payload: bytes = b"\x00\x00\x00\x00") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + payload


def wav_bytes(payload: bytes = b"\x00" * 32) -> bytes:
    chunk = b"WAVEfmt " + payload
    return b"RIFF" + struct.pack("<I", len(chunk)) + chunk


SOURCE_PLATFORM = "采集平台批次"
SOURCE_TEXT = "预训练文本产线交付"
SOURCE_IMAGE = "预训练图像产线交付"
SOURCE_AUDIO = "预训练音频产线交付"
SOURCE_VIDEO = "预训练视频产线交付"

BATCH = "BATCH-20261001-01"


def record(sample_id=None, *, source, files, source_id="SRC-DEMO-2026",
           batch_id=BATCH, upstream_version="pipeline-1.4.2", license="可训练",
           extra=None, drop=()):
    payload = {
        "sample_id": sample_id,
        "source": source,
        "source_id": source_id,
        "batch_id": batch_id,
        "upstream_version": upstream_version,
        "license": license,
        "files": files,
        "processing": {"00": {"status": "uploaded"}},
        "qc": {"verdict": None},
    }
    if extra:
        payload.update(extra)
    for key in drop:
        payload.pop(key, None)
    return payload


def build_records():
    return [
        # 1) 采集平台批次 + 本地 PNG（相对路径 → 走本地文件头校验）
        record("demo-img-local", source=SOURCE_PLATFORM,
               files=[{"modality": "image", "path": "media/demo-img-local.png",
                       "size_bytes": 16}]),
        # 2) 图像产线 + 远端链接
        record("demo-img-oss", source=SOURCE_IMAGE,
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-img-oss.jpg"}]),
        # 3) 音频产线
        record("demo-audio", source=SOURCE_AUDIO,
               files=[{"modality": "audio", "oss_uri": "oss://demo/aud/demo-audio.wav"}]),
        # 4) 视频产线
        record("demo-video", source=SOURCE_VIDEO,
               files=[{"modality": "video", "oss_uri": "oss://demo/vid/demo-video.mp4"}]),
        # 5) 文本产线（json 属于文本白名单）
        record("demo-text", source=SOURCE_TEXT,
               files=[{"modality": "text", "oss_uri": "oss://demo/txt/demo-text.json"}]),
        # 6) 多文件记录：同一 sample 的图像 + 音频
        record("demo-multi", source=SOURCE_PLATFORM,
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-multi.png"},
                      {"modality": "audio", "oss_uri": "oss://demo/aud/demo-multi.mp3"}]),
        # 7) 格式不在白名单 → 原始隔离区
        record("demo-bad-format", source=SOURCE_IMAGE,
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-bad-format.gif"}]),
        # 8) 授权不可训练 → 样本隔离库
        record("demo-untrainable", source=SOURCE_IMAGE, license="不可训练",
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-untrainable.png"}]),
        # 9) 缺批次号 → 待补登记
        record("demo-no-batch", source=SOURCE_IMAGE, drop=("batch_id",),
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-no-batch.jpg"}]),
        # 10) 缺 sample_id → 原始隔离区
        record(None, source=SOURCE_IMAGE, drop=("sample_id",),
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-no-id.jpg"}]),
        # 11) 重复登记（与第 2 条同 sample_id + 同链接）→ 待补登记
        record("demo-img-oss", source=SOURCE_IMAGE,
               files=[{"modality": "image", "oss_uri": "oss://demo/img/demo-img-oss.jpg"}]),
    ]


RAW_LINES = ["{这是一个坏掉的 JSON 行", "[\"不是对象\"]"]


def main() -> int:
    media_dir = HERE / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "demo-img-local.png").write_bytes(png_bytes())
    (media_dir / "demo-local-audio.wav").write_bytes(wav_bytes())

    lines = [json.dumps(item, ensure_ascii=False) for item in build_records()]
    lines.extend(RAW_LINES)
    DEMO_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("已生成 %s" % DEMO_PATH)
    print("  记录行=%d（其中坏行=%d）媒体文件=2（media/demo-img-local.png、media/demo-local-audio.wav）"
          % (len(lines), len(RAW_LINES)))
    print("  预期结果：可加工=7（含 1 条本地文件头校验、1 条多文件记录展开 2 项）"
          " 原始隔离=2 样本隔离=1 待补登记=2 坏行=2（坏行台账并入原始隔离文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
