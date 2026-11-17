"""生成算子 #09 准备候选描述的可复现样例。

样例覆盖表1六类描述来源、长度边界、文件名/标签无效、裸图补写和坏行。
媒体仅生成真正可读的 PNG / WAV，不依赖下载任何外部数据。
"""
from __future__ import annotations

import json
import math
import struct
import sys
import wave
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def png_bytes(width: int = 8, height: int = 8, rgb=(200, 120, 60)) -> bytes:
    """纯标准库生成最小合法 PNG，避免引入 OpenCV/PIL 依赖。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_png(name: str, rgb) -> None:
    (HERE / name).write_bytes(png_bytes(rgb=rgb))


def write_wav(name: str, seconds: float = 1.0, freq: float = 440.0, rate: int = 16000) -> None:
    n = int(seconds * rate)
    with wave.open(str(HERE / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)))


LONG_CAPTION = "描述内容" * 200          # 800 字，验证截断到 500


def build_records():
    return [
        # 1) 图注优先于 Alt/标题/字幕/转写/人工补写
        {"sample_id": "sample-0001", "caption": "一只金毛犬在草地上接飞盘",
         "alt": "Alt 备用描述内容示例", "title": "标题备用描述内容示例",
         "image": "img_0001.png", "video": None, "audio": None,
         "metadata": {"source": "sample", "scene": "outdoor"}},
        # 2) 无图注，取 Alt
        {"sample_id": "sample-0002", "caption": None,
         "alt": "一只橘猫趴在沙发上睡觉", "image": "img_0002.png",
         "video": None, "audio": None, "metadata": {}},
        # 3) 仅标题
        {"sample_id": "sample-0003", "title": "海边日落风景照片",
         "image": "img_0003.png", "video": None, "audio": None, "metadata": {}},
        # 4) 音频字幕：分段字幕拼接
        {"sample_id": "sample-0004", "audio": "aud_0004.wav", "image": None,
         "video": None,
         "subtitle": [{"text": "欢迎", "start": 0.0, "end": 1.0},
                      {"text": "观看本期视频内容", "start": 1.0, "end": 3.0}],
         "metadata": {"scene": "studio"}},
        # 5) 语音转写（ASR 结果）
        {"sample_id": "sample-0005",
         "audio": {"path": "aud_0005.wav",
                   "transcript": "今天讲解跨模态数据对齐流程"},
         "image": None, "video": None, "metadata": {}},
        # 6) 图注等于文件名 -> 无效 -> 人工补写任务
        {"sample_id": "sample-0006", "caption": "img_0006", "image": "img_0006.png",
         "video": None, "audio": None, "metadata": {}},
        # 7) 图注等于分类标签 -> 无效 -> 人工补写任务
        {"sample_id": "sample-0007", "caption": "风景照片示例", "image": "img_0007.png",
         "video": None, "audio": None, "metadata": {"image_tags": ["风景照片示例"]}},
        # 8) 裸图：只有通用 text 字段，不属于表1描述来源 -> 补写任务，不调用图像描述模型
        {"sample_id": "sample-0008", "image": "img_0008.png", "video": None, "audio": None,
         "text": "一只小狗在草地上玩耍", "metadata": {}},
        # 9) 图注过短 -> 顺延到 Alt
        {"sample_id": "sample-0009", "caption": "短",
         "alt": "这是一只金毛犬在草地上接飞盘的画面", "image": "img_0009.png",
         "video": None, "audio": None, "metadata": {}},
        # 10) 标题为空 -> 顺延到人工补写
        {"sample_id": "sample-0010", "title": " ", "image": "img_0010.png",
         "video": None, "audio": None,
         "manual_description": "人工补写的描述内容示例", "metadata": {}},
        # 11) 超长图注 -> 截断到 500
        {"sample_id": "sample-0011", "caption": LONG_CAPTION, "image": "img_0011.png",
         "video": None, "audio": None, "metadata": {}},
        # 12) 缺 sample_id -> 拒绝
        {"caption": "缺少 sample_id 的描述文本", "image": "img_0001.png",
         "video": None, "audio": None, "metadata": {}},
    ]


def main() -> None:
    colors = [(60, 180, 60), (180, 120, 60), (60, 60, 180), (200, 200, 60),
              (120, 60, 200), (200, 60, 120)]
    for i, name in enumerate(["img_0001.png", "img_0002.png", "img_0003.png",
                              "img_0006.png", "img_0007.png", "img_0008.png",
                              "img_0009.png", "img_0010.png", "img_0011.png"]):
        write_png(name, colors[i % len(colors)])
    write_wav("aud_0004.wav", seconds=3.0)
    write_wav("aud_0005.wav", seconds=2.0)

    with open(HERE / "demo.jsonl", "w", encoding="utf-8") as f:
        for rec in build_records():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 故意写入一行损坏 JSON，验证异常清单不吞行
        f.write("{this is not json}\n")
    print("sample generated:", HERE)


if __name__ == "__main__":
    main()
