"""生成算子 #12 的样例素材（确定性、可重复、无外部下载）。

产出：
- ``dataset/sample/media/*.png``：标准库生成的纯色 PNG（不引入图像库依赖）；
- ``dataset/sample/pairs.jsonl``：上游 #11 图文候选对格式样例，覆盖
  正常图文对、无图像、无描述、图片缺失、路径穿越、坏行、缺字段、重复 pair_id；
- ``dataset/sample/manifest.json``：可整包上传的素材清单。

用法：python dataset/sample/make_sample.py
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
PAIRS = SAMPLE_DIR / "pairs.jsonl"
MANIFEST = SAMPLE_DIR / "manifest.json"

MEDIA = {
    "golden_retriever.png": (214, 178, 92),
    "cat_sofa.png": (150, 150, 160),
    "car_street.png": (70, 90, 140),
    "red_square.png": (200, 40, 40),
    "empty_scene.png": (240, 240, 240),
}


def png_bytes(rgb, width=32, height=24):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _record(sample_id, batch_id, source_id, text=None, image=None, audio=None,
            metadata=None):
    return {
        "sample_id": sample_id,
        "source_id": source_id,
        "batch_id": batch_id,
        "dataset_version": "demo-10.6",
        "text": ({"content": text, "description_source": "caption",
                  "target_language": "zh"} if text else None),
        "image": ({"path": image, "format": "png"} if image else None),
        "video": None,
        "audio": ({"path": audio, "format": "wav"} if audio else None),
        "metadata": metadata or {},
        "processing": {"09": {"description_source": "caption"}},
    }


def _pair(pair_id, group_id, evidence_type, criterion, value, left, right, priority=1):
    return {
        "pair_id": pair_id,
        "group_id": group_id,
        "modality_pair": sorted({modality for modality in
                                 ("text" if left["text"] or right["text"] else None,
                                  "image" if left["image"] or right["image"] else None,
                                  "audio" if left["audio"] or right["audio"] else None)
                                 if modality}),
        "evidence": {"type": evidence_type, "criterion": criterion,
                     "evidence_source": "rule", "value": value},
        "evidence_priority": priority,
        "left": {"sample_id": left["sample_id"],
                 "modalities": sorted(key for key in ("text", "image", "audio")
                                      if left.get(key)),
                 "record": left},
        "right": {"sample_id": right["sample_id"],
                  "modalities": sorted(key for key in ("text", "image", "audio")
                                       if right.get(key)),
                  "record": right},
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0"},
    }


def build_pairs():
    pairs = []

    pairs.append(_pair(
        "pair-0001", "grp-src-A", "A", "same_source_id", {"source_id": "src-A"},
        _record("s-0001", "batch-01", "src-A",
                text="一只金毛犬在草地上奔跑，嘴里叼着橙色飞盘",
                metadata={"image_tags": ["金毛犬", "草地", "飞盘"]}),
        _record("s-0002", "batch-01", "src-A", image="media/golden_retriever.png")))

    pairs.append(_pair(
        "pair-0002", "grp-src-B", "B", "shared_batch_and_entity",
        {"batch_id": "batch-01", "entity": "汽车"},
        _record("s-0003", "batch-01", "src-B",
                text="一辆蓝色汽车停在城市街道边"),
        _record("s-0004", "batch-01", "src-B", image="media/car_street.png")))

    pairs.append(_pair(
        "pair-0003", "grp-src-C", "C", "temporal_proximity",
        {"window_seconds": 30},
        _record("s-0005", "batch-02", "src-C", text="一只灰猫趴在沙发上"),
        _record("s-0006", "batch-02", "src-C", image="media/cat_sofa.png")))

    # 无图像：图文路线不适用（留产线，不隔离）
    pairs.append(_pair(
        "pair-0004", "grp-src-D", "A", "same_source_id", {"source_id": "src-D"},
        _record("s-0007", "batch-02", "src-D", text="一段旁白描述"),
        _record("s-0008", "batch-02", "src-D", audio="audio/voice.wav")))

    # 有图像但整条配对没有任何描述：不适用图文评分
    pairs.append(_pair(
        "pair-0005", "grp-src-E", "B", "shared_batch_and_entity",
        {"batch_id": "batch-03", "entity": "unknown"},
        _record("s-0009", "batch-03", "src-E", image="media/empty_scene.png"),
        _record("s-0010", "batch-03", "src-E", audio="audio/voice.wav")))

    # 图片缺失：原始文件问题 -> 原始隔离区
    pairs.append(_pair(
        "pair-0006", "grp-src-F", "A", "same_source_id", {"source_id": "src-F"},
        _record("s-0011", "batch-04", "src-F", text="一张不存在的图片"),
        _record("s-0012", "batch-04", "src-F", image="media/not_exists.png")))

    # 路径穿越：原始文件问题 -> 原始隔离区
    pairs.append(_pair(
        "pair-0007", "grp-src-G", "A", "same_source_id", {"source_id": "src-G"},
        _record("s-0013", "batch-04", "src-G", text="非法图片路径"),
        _record("s-0014", "batch-04", "src-G", image="../escape.png")))

    # 同记录内部图文（左记录同时有文本与图像）
    pairs.append(_pair(
        "pair-0008", "grp-src-H", "A", "same_source_id", {"source_id": "src-H"},
        _record("s-0015", "batch-05", "src-H", text="红色方块放在桌面上",
                image="media/red_square.png"),
        _record("s-0016", "batch-05", "src-H", audio="audio/voice2.wav")))
    return pairs


def build_lines():
    """返回 JSONL 行（dict 或原始字符串），包含坏行、缺字段与重复 pair_id。"""
    pairs = build_pairs()
    duplicate = json.loads(json.dumps(pairs[0]))
    missing_pair_id = _pair(
        "", "grp-src-X", "A", "same_source_id", {"source_id": "src-X"},
        _record("s-0017", "batch-06", "src-X", text="缺 pair_id 的配对"),
        _record("s-0018", "batch-06", "src-X", image="media/red_square.png"))
    missing_pair_id.pop("pair_id")
    return [
        pairs[0], pairs[1], pairs[2], pairs[3], pairs[4],
        pairs[5], pairs[6],
        "{这里不是合法 JSON}",
        missing_pair_id,
        duplicate,
        pairs[7],
    ]


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for name, rgb in MEDIA.items():
        (MEDIA_DIR / name).write_bytes(png_bytes(rgb))

    lines = build_lines()
    with open(str(PAIRS), "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write((line if isinstance(line, str)
                          else json.dumps(line, ensure_ascii=False)) + "\n")

    manifest = {
        "format": "crossmodal-manifest-v1",
        "operator": "generate_candidate_pairs",
        "entrypoint": "pairs.jsonl",
        "artifacts": [{"path": "pairs.jsonl", "role": "main", "records": len(lines)}],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("已生成样例：%s（%d 行）、媒体 %d 个、%s"
          % (PAIRS, len(lines), len(MEDIA), MANIFEST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
