#!/usr/bin/env python
"""生成算子 14 的可复现样例（不联网、不依赖大数据集）。

产物（与本脚本同目录）：
- ``demo.jsonl``：7 条视文记录，覆盖正常配对、多语义段、扁平记录，
  以及「无视频 / 无抽帧 / 缺描述 / 帧文件缺失」等边界；
- ``frames/*.png``：用 zlib 合成的**真实可解码** PNG（纯色，仅验证管线，
  不含任何真实视频内容，也不用于证明模型精度）。

运行：``python dataset/sample/make_sample.py``（幂等，可重复执行）。
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FPS = 2.0


def png_bytes(width: int = 32, height: int = 24, rgb=(180, 60, 60)) -> bytes:
    """用 zlib + struct 合成一张标准 PNG（RGB，无压缩过滤），保证 PIL 可解码。"""
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


PALETTE = [
    (190, 70, 60), (60, 150, 90), (70, 90, 190), (190, 170, 60),
    (150, 70, 170), (60, 170, 180), (170, 110, 60), (110, 110, 110),
]


def write_frames(prefix: str, count: int, *, start_index: int = 1,
                 fps: float = FPS) -> list:
    """写出 ``frames/<prefix>_NNNN.png`` 并返回帧描述列表。"""
    target_dir = BASE_DIR / "frames"
    target_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for offset in range(count):
        index = start_index + offset
        name = "%s_%04d.png" % (prefix, index)
        rgb = PALETTE[(index - 1) % len(PALETTE)]
        (target_dir / name).write_bytes(png_bytes(rgb=rgb))
        frames.append({
            "frame_index": index,
            "timestamp": round(offset / fps, 3),
            "path": "frames/%s" % name,
            "width": 32,
            "height": 24,
        })
    return frames


def video_payload(path: str, frames: list, *, duration: float, segments=None) -> dict:
    return {
        "path": path,
        "duration": duration,
        "fps": FPS,
        "frames": frames,
        "segments": segments or [],
    }


def text_payload(content: str, *, lang: str = "zh") -> dict:
    return {"content": content, "original": content, "lang": lang}


def pair_record(pair_id: str, description: str, video: dict,
                text_record_overrides=None) -> dict:
    """#11 ``11_candidate_pairs.jsonl`` 形态：一侧视频、一侧文本。"""
    video_record = {
        "sample_id": "%s-video" % pair_id,
        "source_id": "src-demo-A",
        "batch_id": "batch-demo",
        "video": video,
        "image": None,
        "text": None,
        "processing": {},
        "metadata": {},
    }
    text_record = {
        "sample_id": "%s-text" % pair_id,
        "source_id": "src-demo-A",
        "batch_id": "batch-demo",
        "video": None,
        "image": None,
        "text": text_payload(description),
        "processing": {},
        "metadata": {"description_source": "human_caption"},
    }
    if text_record_overrides:
        text_record.update(text_record_overrides)
    return {
        "pair_id": pair_id,
        "group_id": "grp-src-demo-A",
        "modality_pair": ["text", "video"],
        "evidence": {"type": "A", "criterion": "same_source_id",
                     "value": {"source_id": "src-demo-A"}},
        "evidence_priority": 1,
        "left": {"sample_id": video_record["sample_id"],
                 "modalities": ["text", "video"], "record": video_record},
        "right": {"sample_id": text_record["sample_id"],
                  "modalities": ["text", "video"], "record": text_record},
        "producer": {"operator": "generate_candidate_pairs",
                     "operator_version": "v1.0.0", "uniqueId": "sample_file"},
    }


def flat_record(sample_id: str, description: str, video: dict) -> dict:
    """扁平形态：同一记录内同时含抽帧序列与文本描述。"""
    return {
        "sample_id": sample_id,
        "source_id": "src-demo-B",
        "batch_id": "batch-demo",
        "video": video,
        "image": None,
        "text": text_payload(description) if description is not None else None,
        "processing": {},
        "metadata": {"description_source": "human_caption"} if description else {},
    }


def build_records() -> list:
    records = []

    # 1) 正常配对：3 帧整段评分
    frames = write_frames("pair-001", 3)
    records.append(pair_record(
        "pair-001", "一只橘色小猫在木地板上追逐毛线球。",
        video_payload("video/pair-001.mp4", frames, duration=1.5)))

    # 2) 多语义段：整段视频合计最多 8 帧（两段共享预算 4+4），逐段判定
    #    （任一段段分低于阈值即整段不通过）
    frames = write_frames("pair-002", 8)
    segments = [
        {"start": 0.0, "end": 2.0, "text": "第一段：运动员在跑道上起跑加速。",
         "frames": [frames[0], frames[1], frames[2], frames[3]]},
        {"start": 2.0, "end": 4.0, "text": "第二段：运动员冲过终点线后缓慢减速。",
         "frames": [frames[4], frames[5], frames[6], frames[7]]},
    ]
    records.append(pair_record(
        "pair-002", "田径比赛全过程记录。",
        video_payload("video/pair-002.mp4", frames, duration=4.0, segments=segments)))

    # 3) 扁平记录：2 帧
    frames = write_frames("flat-003", 2)
    records.append(flat_record(
        "flat-003", "雨天的城市街道上车辆缓慢通行。",
        video_payload("video/flat-003.mp4", frames, duration=1.0)))

    # 4) 无视频引用 → 流程状态（待处理，不隔离）
    records.append(flat_record("flat-004", "没有视频引用的文本描述。", None))

    # 5) 有视频但没有任何抽帧 → 流程状态（待处理，不隔离）
    records.append(flat_record(
        "flat-005", "视频尚未抽帧，等待上游关键帧算子产出。",
        video_payload("video/flat-005.mp4", [], duration=2.0)))

    # 6) 引用了不存在的抽帧 → 原始文件问题（原始隔离区）
    frames = write_frames("flat-006", 1)
    frames.append({"frame_index": 2, "timestamp": 1.0,
                   "path": "frames/flat-006_0002.png", "width": 32, "height": 24})
    records.append(flat_record(
        "flat-006", "抽帧清单里引用了并不存在的帧文件。",
        video_payload("video/flat-006.mp4", frames, duration=1.5)))

    # 7) 缺少文本描述 → 流程状态（待处理，不隔离）
    frames = write_frames("flat-007", 2)
    records.append(flat_record(
        "flat-007", None, video_payload("video/flat-007.mp4", frames, duration=1.0)))

    return records


def main() -> int:
    records = build_records()
    target = BASE_DIR / "demo.jsonl"
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    frame_files = sorted((BASE_DIR / "frames").glob("*.png"))
    print("已生成 %s：%d 条记录，%d 张抽帧" % (target.name, len(records), len(frame_files)))
    print("提示：默认配置 enabled=false（表1），CLI 冒烟为原样透传；"
          "启用评分需本地 CLIP-ViT/B-32 权重（--enabled --clip-weights <dir>）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
