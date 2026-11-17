"""生成算子 #11 生成候选配对 的可重复样例：6 个真实媒体文件 + 13 行输入清单。

样例覆盖：
- 证据 A（同来源编号）一对多拆分、共享 group_id；
- 证据 B（时间重叠，文本时间索引与音频时长）；
- 证据 C（共同关键词阈值边界，2 个通过 / 1 个不通过）；
- 坏 JSON 行、缺 sample_id、无模态、重复 sample_id、上游未准入标记；
- 未配对记录（无候选，留清单不隔离）。

媒体全部用标准库生成（PNG 手工编码 + wave 写 WAV），不依赖 OpenCV/Pillow/ffmpeg。
视频示例不在本样例中提供：仓库外挂编码器才能产出可解码 MP4，
视频时间范围与携带路径由 tests/test_pairing.py、tests/test_operator.py 用合成记录覆盖。
"""
from __future__ import annotations

import json
import math
import struct
import wave
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def write_png(name: str, rgb: tuple, width: int = 32, height: int = 24) -> None:
    """写一张纯色 PNG（标准库实现，避免引入图像库依赖）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
               chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    (HERE / name).write_bytes(payload)


def write_wav(name: str, seconds: float, freq: float = 440.0, rate: int = 16000) -> None:
    frames = int(seconds * rate)
    with wave.open(str(HERE / name), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(frames)
        ))


def build_samples() -> list:
    return [
        # 1-3：同一来源编号 src-A，应形成证据 A 的一对多拆分（共享 group_id）
        {"sample_id": "sample-0001", "source_id": "src-A", "batch_id": "batch-01",
         "scene_id": "outdoor",
         "text": {"content": "一只金毛犬在草地上接飞盘", "original": "一只金毛犬在草地上接飞盘",
                  "tokens": ["金毛犬", "草地", "飞盘"], "lang": "zh"},
         "image": {"path": "golden_retriever_grass.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None,
         "processing": {}, "alignment": {"whole": None, "local": []}, "qc": {},
         "metadata": {"image_tags": ["金毛犬", "飞盘"]}},
        {"sample_id": "sample-0002", "source_id": "src-A", "batch_id": "batch-01",
         "scene_id": "outdoor",
         "text": {"content": "金毛犬在草地上玩飞盘", "tokens": ["金毛犬", "草地", "飞盘"],
                  "lang": "zh"},
         "image": {"path": "cat_sofa.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None,
         "metadata": {"image_tags": ["金毛犬", "飞盘"]}},
        {"sample_id": "sample-0003", "source_id": "src-A", "batch_id": "batch-01",
         "scene_id": "indoor",
         "text": {"content": "金毛犬趴在室内地板上", "tokens": ["金毛犬", "室内", "地板"],
                  "lang": "zh"},
         "image": {"path": "golden_retriever_grass.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None, "metadata": {}},

        # 4-5：来源不同、无时间，只能靠共同关键词（2 个）形成证据 C
        {"sample_id": "sample-0004", "source_id": "src-B", "batch_id": "batch-01",
         "scene_id": "street",
         "text": {"content": "汽车行驶在城市街道上", "tokens": ["汽车", "街道"], "lang": "zh"},
         "image": {"path": "car_street.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None, "metadata": {}},
        {"sample_id": "sample-0005", "source_id": "src-C", "batch_id": "batch-01",
         "scene_id": "crossing",
         "text": {"content": "街道上的汽车等待红绿灯",
                  "tokens": ["汽车", "街道", "红绿灯"], "lang": "zh"},
         "image": {"path": "car_street.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None, "metadata": {}},

        # 6-7：不同来源、关键词不重叠，但都有真实时间范围 → 证据 B（时间重叠）
        {"sample_id": "sample-0006", "source_id": "src-D", "batch_id": "batch-02",
         "scene_id": "audio-bound",
         "text": {"content": "车流环境音转写片段", "tokens": ["车流", "环境音"],
                  "time_index": [0.0, 2.0], "lang": "zh"},
         "image": None, "video": None,
         "audio": {"path": "traffic_ambient.wav", "duration": 2.0, "sample_rate": 16000,
                   "channels": 1, "segments": []},
         "metadata": {}},
        {"sample_id": "sample-0007", "source_id": "src-E", "batch_id": "batch-02",
         "scene_id": "street-noise",
         "text": {"content": "街景环境声转写片段", "tokens": ["街景", "环境声"],
                  "time_index": [0.5, 2.5], "lang": "zh"},
         "image": None, "video": None,
         "audio": {"path": "street_ambient.wav", "duration": 2.0, "sample_rate": 16000,
                   "channels": 1, "segments": []},
         "metadata": {}},

        # 8：与 sample-0004 只共享 1 个关键词（低于默认阈值 2）→ 未配对清单
        {"sample_id": "sample-0008", "source_id": "src-F", "batch_id": "batch-03",
         "scene_id": "lab",
         "text": {"content": "汽车维护手册的一页说明", "tokens": ["汽车", "维护"], "lang": "zh"},
         "image": {"path": "quantum_instrument.png", "width": 32, "height": 24, "format": "PNG"},
         "video": None, "audio": None, "metadata": {}},

        # 9：坏 JSON 行
        "{not json}",

        # 10：缺 sample_id
        {"source_id": "src-G", "text": {"content": "缺少样本编号的记录"},
         "image": {"path": "cat_sofa.png"}, "video": None, "audio": None, "metadata": {}},

        # 11：没有任何模态
        {"sample_id": "sample-0009", "source_id": "src-H", "text": None, "image": None,
         "video": None, "audio": None, "metadata": {}},

        # 12：重复 sample_id（防御性校验，正常调用不应出现）
        {"sample_id": "sample-0001", "source_id": "src-Z", "scene_id": "dup",
         "text": {"content": "重复样本编号"}, "image": {"path": "cat_sofa.png"},
         "video": None, "audio": None, "metadata": {}},

        # 13：上游准入标记为未通过（防御性校验）
        {"sample_id": "sample-0010", "source_id": "src-I",
         "text": {"content": "上游标记未准入"}, "image": {"path": "cat_sofa.png"},
         "video": None, "audio": None,
         "admission": {"passed": False, "reason": "unit-test"}, "metadata": {}},
    ]


def main() -> None:
    write_png("golden_retriever_grass.png", (60, 180, 60))
    write_png("cat_sofa.png", (180, 120, 60))
    write_png("car_street.png", (60, 60, 180))
    write_png("quantum_instrument.png", (120, 120, 120))
    write_wav("traffic_ambient.wav", 2.0, freq=220.0)
    write_wav("street_ambient.wav", 2.0, freq=330.0)

    target = HERE / "samples.jsonl"
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        for item in build_samples():
            line = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            handle.write(line + "\n")
    print("sample generated:", HERE)


if __name__ == "__main__":
    main()
