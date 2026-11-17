"""生成算子 18 的可复现样例数据（**真实可解码 PNG**，不依赖任何外部下载）。

产物：
- ``media/*.png``        三张 64×48 纯色 PNG + 一个刻意损坏的 ``broken.png``
- ``demo.jsonl``         默认路线（``enabled: false``）样例，含坏行/空行/缺字段
- ``demo_enabled.jsonl`` 开启路线样例，覆盖预填/待人工/隔离/原始隔离四类分支
- ``manifest.json``      文件清单（sha256 + 用途），供交付与复现核对

用法::

    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent


def png_bytes(width: int = 64, height: int = 48, rgb=(200, 40, 40)) -> bytes:
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def box(box_id: str, x: int, y: int, w: int, h: int,
        label: str, confidence: float) -> Dict[str, Any]:
    return {"box_id": box_id,
            "bbox": {"x": x, "y": y, "w": w, "h": h, "coord": "xywh-pixel"},
            "class_name": label,
            "confidence": confidence,
            "source": "operator_17"}


def main() -> int:
    media_dir = BASE_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    (media_dir / "shot_001.png").write_bytes(png_bytes(rgb=(210, 40, 40)))
    (media_dir / "shot_002.png").write_bytes(png_bytes(rgb=(40, 210, 40)))
    (media_dir / "shot_003.png").write_bytes(png_bytes(rgb=(40, 40, 210)))
    # 刻意损坏的媒体：用于验证「原始文件-损坏」分支（不做任何静默兜底）
    (media_dir / "broken.png").write_bytes(b"this-is-not-a-valid-image")

    # ---------------- 默认路线样例（工具关闭：原样放行，不产细粒度对应） ------- #
    demo: List[Any] = [
        {"sample_id": "sample-0001", "batch_id": "demo-batch",
         "modalities": ["image", "text"],
         "text": {"content": "金毛犬在草地上接飞盘", "lang": "zh"},
         "image": "media/shot_001.png",
         "regions": [box("b1", 2, 2, 30, 24, "dog", 0.91),
                     box("b2", 34, 8, 24, 20, "frisbee", 0.77),
                     box("b3", 2, 30, 56, 16, "grass", 0.62)]},
        {"sample_id": "sample-0002",
         "text": {"content": "一只橘猫趴在沙发上"},
         "image": "media/shot_002.png",
         "regions": [{"box_id": "b1", "bbox": [4, 4, 36, 30], "class_name": "cat"},
                     {"box_id": "b2", "bbox": [4, 32, 60, 46], "class_name": "sofa"}]},
        {"sample_id": "sample-0003",
         "text": {"description": "蓝色杯子放在木桌上"},
         "image": "media/shot_003.png",
         "regions": [{"box_id": "b1", "bbox": [0.05, 0.10, 0.80, 0.90],
                      "coord": "xyxy-normalized", "class_name": "cup"}]},
        {"sample_id": "sample-0004",
         "text": {"content": "画面里有一只白色小狗"},
         "image": {"path": "media/shot_001.png",
                   "regions": [box("r1", 6, 6, 28, 22, "puppy", 0.88)]}},
        {"sample_id": "sample-0005",
         "text": {"content": "红色的球在右边"},
         "processing": {"17": {"regions": [box("b1", 40, 20, 20, 20, "ball", 0.71)]}},
         "image": "media/shot_002.png"},
        {"sample_id": "sample-0006",
         "text": {"content": "金毛犬叼着飞盘", "lang": "zh",
                  "tokens": ["金毛犬", "飞盘"]},
         "image": "media/shot_001.png",
         "regions": [box("b1", 2, 2, 30, 24, "dog", 0.9),
                     box("b2", 36, 10, 22, 18, "frisbee", 0.8)]},
        {"sample_id": "sample-0007",
         "text": {"content": "尚未生成候选框的样本"},
         "image": "media/shot_003.png",
         "regions": []},
        {"pair_id": "pair-0008",
         "left": {"sample_id": "sample-0008-text",
                  "record": {"text": {"content": "小狗在草地上奔跑"}}},
         "right": {"sample_id": "sample-0008-image",
                   "record": {"image": "media/shot_001.png",
                              "regions": [box("b1", 2, 20, 40, 26, "dog", 0.83)]}}},
        {"sample_id": "sample-0009",
         "image": "media/shot_002.png",
         "regions": [box("b1", 4, 4, 30, 26, "unknown", 0.4)]},
        {"sample_id": "sample-0010",
         "text": {"content": "只有描述没有图像的样本"}},
        # 坏行与空行：必须进拒绝清单，不得静默丢弃
        "{这不是合法 JSON",
        "[1, 2, 3]",
        "",
    ]

    # ---------------- 开启路线样例（enabled: true） ------------------------- #
    enabled: List[Any] = [
        {"sample_id": "enabled-0001",
         "text": {"content": "金毛犬在草地上接飞盘"},
         "image": "media/shot_001.png",
         "regions": [box("b1", 2, 2, 30, 24, "dog", 0.91),
                     box("b2", 34, 8, 24, 20, "frisbee", 0.77),
                     box("b3", 2, 30, 56, 16, "grass", 0.62)]},
        {"sample_id": "enabled-0002",
         "text": {"content": "橘猫与沙发", "tokens": ["橘猫", "沙发"]},
         "image": "media/shot_002.png",
         "regions": [box("b1", 4, 4, 36, 30, "cat", 0.9),
                     box("b2", 4, 32, 60, 46, "sofa", 0.7)]},
        # 描述整体等于文件名 → 工具开启后没有对应关系 → 样本隔离库
        {"sample_id": "enabled-0003",
         "text": {"content": "shot_003.png"},
         "image": "media/shot_003.png",
         "regions": [box("b1", 4, 4, 30, 26, "object", 0.5)]},
        # 声明的图像不存在 → 原始隔离区
        {"sample_id": "enabled-0004",
         "text": {"content": "引用了一个不存在的图像"},
         "image": "media/missing.png",
         "regions": [box("b1", 4, 4, 30, 26, "object", 0.5)]},
        # 损坏图像 → 原始隔离区（损坏文件已随样例提供）
        {"sample_id": "enabled-0005",
         "text": {"content": "损坏图像样本"},
         "image": "media/broken.png",
         "regions": [box("b1", 4, 4, 30, 26, "object", 0.5)]},
        # 候选框坐标非法 → 无可用框 → 流程状态（给人工空白列表）
        {"sample_id": "enabled-0006",
         "text": {"content": "候选框非法样本"},
         "image": "media/shot_001.png",
         "regions": [{"box_id": "bad", "bbox": {"x": -3, "y": 0, "w": 10, "h": 10}}]},
        # 缺描述 → 流程状态（留产线待补）
        {"sample_id": "enabled-0007",
         "image": "media/shot_001.png",
         "regions": [box("b1", 2, 2, 30, 24, "dog", 0.9)]},
        # 标识重复：第二条必须进原始隔离区
        {"sample_id": "enabled-0008",
         "text": {"content": "重复标识样本"},
         "image": "media/shot_002.png",
         "regions": [box("b1", 2, 2, 30, 24, "object", 0.6)]},
        {"sample_id": "enabled-0008",
         "text": {"content": "重复标识样本（重复行）"},
         "image": "media/shot_002.png",
         "regions": [box("b1", 2, 2, 30, 24, "object", 0.6)]},
    ]

    demo_path = BASE_DIR / "demo.jsonl"
    enabled_path = BASE_DIR / "demo_enabled.jsonl"
    _write_jsonl(demo_path, demo)
    _write_jsonl(enabled_path, enabled)

    files = sorted(path for path in BASE_DIR.rglob("*") if path.is_file())
    manifest = {
        "format": "crossmodal-sample-manifest-v1",
        "operator": "phrase_region_annotation",
        "operator_number": 18,
        "generated_by": "dataset/sample/make_sample.py",
        "note": "全部媒体均为脚本合成的真实可解码 PNG；不含任何外部下载数据",
        "datasets": [
            {"path": demo_path.name, "route": "enabled=false",
             "description": "默认路线样例：原样放行，不产生细粒度对应",
             "records": len([item for item in demo if isinstance(item, dict)]),
             "bad_lines": 2, "blank_lines": 1},
            {"path": enabled_path.name, "route": "enabled=true",
             "description": "开启路线样例：覆盖预填/待人工/样本隔离/原始隔离",
             "records": len(enabled), "bad_lines": 0, "blank_lines": 0},
        ],
        "media": [{"path": path.relative_to(BASE_DIR).as_posix(),
                   "size": path.stat().st_size,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in files
                  if path.suffix.lower() in {".png", ".jpg", ".jpeg"}],
    }
    (BASE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("样例生成完成：%s" % BASE_DIR)
    print("  demo.jsonl         记录 %d 条 + 坏行 2 + 空行 1"
          % len([item for item in demo if isinstance(item, dict)]))
    print("  demo_enabled.jsonl 记录 %d 条" % len(enabled))
    print("  media/*.png        %d 个文件" % len(manifest["media"]))
    return 0


def _write_jsonl(path: Path, rows: List[Any]) -> None:
    lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
             for item in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
