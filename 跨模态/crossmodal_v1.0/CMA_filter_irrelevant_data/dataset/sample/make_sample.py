"""生成初筛算子 demo 样例：3 张图像、1 段视频、2 段音频、8 条样本记录。"""
import json
import math
import struct
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent


def make_image(name: str, color_bgr: tuple) -> None:
    img = np.full((240, 320, 3), color_bgr, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)  # imwrite 对中文路径静默失败，改用 imencode
    if not ok:
        raise RuntimeError(f"imencode failed: {name}")
    buf.tofile(str(HERE / name))


def make_wav(name: str, seconds: float, freq: float = 440.0, rate: int = 16000) -> None:
    n = int(seconds * rate)
    with wave.open(str(HERE / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)
        )
        w.writeframes(frames)


def make_video(name: str, seconds: float = 2.0, fps: int = 10) -> None:
    path = str(HERE / name)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for i in range(int(seconds * fps)):
        frame = np.full((240, 320, 3), (30 + i * 10) % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def main() -> None:
    make_image("golden_retriever_grass.jpg", (60, 180, 60))
    make_image("cat_sofa.jpg", (180, 120, 60))
    make_image("car_street.jpg", (60, 60, 180))
    make_video("street_traffic.mp4", seconds=2.0)
    make_wav("traffic_ambient.wav", seconds=2.0)
    make_wav("short_noise.wav", seconds=0.5)

    samples = [
        {"sample_id": "sample-0001", "text": "一只金毛犬在草地上接飞盘",
         "image": "golden_retriever_grass.jpg", "video": None, "audio": None,
         "metadata": {"source": "sample", "scene": "outdoor"}},
        {"sample_id": "sample-0002", "text": "橘猫趴在沙发上睡觉",
         "image": "cat_sofa.jpg", "video": None, "audio": None, "metadata": {}},
        {"sample_id": "sample-0003", "text": "量子计算的最新研究进展",
         "image": "golden_retriever_grass.jpg", "video": None, "audio": None,
         "metadata": {"note": "文本与图像明显无关，应被排除"}},
        {"sample_id": "sample-0004", "text": None, "image": None,
         "video": "street_traffic.mp4", "audio": "traffic_ambient.wav",
         "metadata": {"note": "视频音频时长一致，应保留"}},
        {"sample_id": "sample-0005", "text": None, "image": None,
         "video": "street_traffic.mp4", "audio": "short_noise.wav",
         "metadata": {"note": "时长差 75%，应被排除"}},
        {"sample_id": "sample-0006", "text": "一辆汽车停在街道旁",
         "image": "不存在的图.jpg", "video": None, "audio": None,
         "metadata": {"note": "图像文件缺失，应被排除"}},
        {"sample_id": "sample-0007", "text": "仅文本样本，无适用初筛规则，透传保留",
         "image": None, "video": None, "audio": None, "metadata": {}},
        {"text": "缺少 sample_id 的记录，应被排除", "image": "cat_sofa.jpg",
         "video": None, "audio": None, "metadata": {}},
    ]
    with open(HERE / "samples.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print("sample generated:", HERE)


if __name__ == "__main__":
    main()
