"""生成算子 #02 核验场景编号 的可重复样例（纯标准库、无随机数、固定字节）。

产物：
- ``demo.jsonl``（16 行，含正常 / 边界 / 异常行）
- ``manifest.json``（crossmodal-manifest-v1）
- ``media/*.png|wav|mp4``（本地合成的固定字节媒体）

样例覆盖：
- 场景 scene-A：3 条记录，模态并集 4 种 → 融合集核验通过；
- 场景 scene-B：2 条纯文本 → 模态不足，改走配对集；
- 场景 scene-C：1 条纯图像 → 模态不足；
- 场景 scene-D：由 ``group_id`` 提供，1 条 text+audio → 通过；
- 无场景编号：4 条（含 1 条三个模态、1 条引用缺失媒体、1 条引用越界媒体）；
- 重复 ``sample_id``、缺 ``sample_id``、坏 JSON、非对象行、空行。

执行：``python dataset/sample/make_sample.py``
"""
from __future__ import annotations

import json
import struct
import wave
import zlib
from pathlib import Path

#: 样例主入口名
ENTRYPOINT = "demo.jsonl"
MANIFEST_FORMAT = "crossmodal-manifest-v1"


# ------------------------------------------------------------------ 媒体合成
def _png_bytes(width: int = 4, height: int = 4, seed: int = 0) -> bytes:
    """生成确定性 PNG（无 Pillow 依赖）。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            raw.extend(((x * 30 + seed) % 256, (y * 40 + seed) % 256, (seed * 17) % 256))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _wav_bytes(sample_rate: int = 16000, seconds: float = 0.1, freq: int = 440) -> bytes:
    """生成确定性 16kHz 单声道 WAV。"""
    import io
    import math

    frames = int(sample_rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            value = int(12000 * math.sin(2 * math.pi * freq * index / sample_rate))
            payload.extend(struct.pack("<h", value))
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


def _mp4_bytes() -> bytes:
    """生成确定性伪 MP4 容器（本算子只按相对路径搬运，不解码视频）。"""
    payload = b"crossmodal-demo-video\x00" * 8
    box = struct.pack(">I", 8 + len(payload)) + b"ftyp" + payload
    return box


# ------------------------------------------------------------------ 记录构造
def _text(content, tokens):
    return {"content": content, "original": content, "tokens": tokens, "lang": "zh"}


def _image(path, seed=0):
    return {"path": path, "width": 4, "height": 4, "format": "PNG", "seed": seed}


def _video(path):
    return {"path": path, "duration": 1.0}


def _audio(path):
    return {"path": path, "sample_rate": 16000, "channels": 1, "duration": 0.1}


#: 媒体文件 → 生成器
MEDIA_FILES = {
    "media/img_a.png": lambda: _png_bytes(seed=1),
    "media/img_b.png": lambda: _png_bytes(seed=2),
    "media/img_c.png": lambda: _png_bytes(seed=3),
    "media/img_d.png": lambda: _png_bytes(seed=4),
    "media/img_e.png": lambda: _png_bytes(seed=5),
    "media/img_f.png": lambda: _png_bytes(seed=6),
    "media/clip_v.mp4": _mp4_bytes,
    "media/aud_a.wav": lambda: _wav_bytes(freq=440),
    "media/aud_b.wav": lambda: _wav_bytes(freq=523),
    "media/aud_c.wav": lambda: _wav_bytes(freq=659),
}


def sample_lines():
    """返回样例文件的 16 行文本（含空行与坏行）。"""
    lines = [
        # 1 scene-A：text + image
        json.dumps({
            "sample_id": "s-001", "source_id": "src-1", "batch_id": "batch-01",
            "scene_id": "scene-A", "authorization": "trainable",
            "text": _text("一只金毛犬在草地上接飞盘", ["金毛犬", "草地", "飞盘"]),
            "image": _image("media/img_a.png", 1), "video": None, "audio": None,
            "capture_platform": "capture-01", "device_concurrency": 4,
            "metadata": {"caption": "一只金毛犬在草地上接飞盘"},
        }, ensure_ascii=False),
        # 2 scene-A：video + audio（补齐到 4 种模态）
        json.dumps({
            "sample_id": "s-002", "source_id": "src-1", "batch_id": "batch-01",
            "scene_id": "scene-A", "authorization": "trainable",
            "text": None, "image": None,
            "video": _video("media/clip_v.mp4"), "audio": _audio("media/aud_a.wav"),
            "metadata": {"device_failover": "readonly"},
        }, ensure_ascii=False),
        # 3 scene-B：纯文本
        json.dumps({
            "sample_id": "s-003", "source_id": "src-2", "batch_id": "batch-01",
            "scene_id": "scene-B", "text": _text("橘猫趴在窗台上", ["橘猫", "窗台"]),
            "image": None, "video": None, "audio": None, "metadata": {},
        }, ensure_ascii=False),
        # 4 scene-B：纯文本
        json.dumps({
            "sample_id": "s-004", "source_id": "src-2", "batch_id": "batch-01",
            "scene_id": "scene-B", "text": _text("乙炔钢瓶需要远离火源", ["乙炔", "钢瓶"]),
            "image": None, "video": None, "audio": None, "metadata": {},
        }, ensure_ascii=False),
        # 5 scene-C：纯图像
        json.dumps({
            "sample_id": "s-005", "source_id": "src-3", "batch_id": "batch-01",
            "scene_id": "scene-C", "text": None,
            "image": _image("media/img_b.png", 2), "video": None, "audio": None,
            "metadata": {},
        }, ensure_ascii=False),
        # 6 无场景编号：text + image
        json.dumps({
            "sample_id": "s-006", "source_id": "src-4", "batch_id": "batch-01",
            "text": _text("输电铁塔上的绝缘子", ["输电铁塔", "绝缘子"]),
            "image": _image("media/img_c.png", 3), "video": None, "audio": None,
            "metadata": {},
        }, ensure_ascii=False),
        # 7 无场景编号：纯图像
        json.dumps({
            "sample_id": "s-007", "source_id": "src-4", "batch_id": "batch-01",
            "text": None, "image": _image("media/img_d.png", 4), "video": None,
            "audio": None, "metadata": {},
        }, ensure_ascii=False),
        # 8 group_id = scene-D：text + audio
        json.dumps({
            "sample_id": "s-008", "source_id": "src-5", "batch_id": "batch-01",
            "group_id": "scene-D", "text": _text("变压器嗡嗡作响", ["变压器", "嗡嗡"]),
            "image": None, "video": None, "audio": _audio("media/aud_b.wav"),
            "metadata": {},
        }, ensure_ascii=False),
        # 9 metadata.scene_id = scene-A：text + image
        json.dumps({
            "sample_id": "s-009", "source_id": "src-1", "batch_id": "batch-01",
            "text": _text("金毛犬接住飞盘后摇尾巴", ["金毛犬", "飞盘", "尾巴"]),
            "image": _image("media/img_e.png", 5), "video": None, "audio": None,
            "metadata": {"scene_id": "scene-A"},
        }, ensure_ascii=False),
        # 10 无场景编号：媒体缺失 + 媒体路径越界
        json.dumps({
            "sample_id": "s-010", "source_id": "src-6", "batch_id": "batch-01",
            "text": None, "image": _image("media/img_missing.png", 0),
            "video": _video("C:/abs/clip.mp4"), "audio": None, "metadata": {},
        }, ensure_ascii=False),
        # 11 重复 sample_id（s-001）
        json.dumps({
            "sample_id": "s-001", "source_id": "src-1", "batch_id": "batch-01",
            "scene_id": "scene-A", "text": _text("重复上报的金毛犬记录", ["金毛犬"]),
            "image": _image("media/img_a.png", 1), "video": None, "audio": None,
            "metadata": {},
        }, ensure_ascii=False),
        # 12 缺 sample_id
        json.dumps({"text": _text("没有样本编号", ["缺失"])}, ensure_ascii=False),
        # 13 坏 JSON
        '{"sample_id": "s-broken", ',
        # 14 非对象行
        '[1, 2, 3]',
        # 15 空行
        '',
        # 16 无场景编号：text + image + audio
        json.dumps({
            "sample_id": "s-011", "source_id": "src-7", "batch_id": "batch-01",
            "text": _text("巡检人员记录设备温度", ["巡检", "设备", "温度"]),
            "image": _image("media/img_f.png", 6), "video": None,
            "audio": _audio("media/aud_c.wav"), "metadata": {},
        }, ensure_ascii=False),
    ]
    return lines


def build_sample(directory) -> Path:
    """在 directory 下生成 demo.jsonl / manifest.json / media/*。"""
    base = Path(directory)
    media_root = base / "media"
    media_root.mkdir(parents=True, exist_ok=True)

    for relative, builder in MEDIA_FILES.items():
        target = base / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = builder()
        if not target.exists() or target.read_bytes() != payload:
            target.write_bytes(payload)

    entrypoint = base / ENTRYPOINT
    entrypoint.write_text("".join("%s\n" % line for line in sample_lines()),
                          encoding="utf-8")

    manifest = {
        "format": MANIFEST_FORMAT,
        "entrypoint": ENTRYPOINT,
        "artifacts": ([{"path": ENTRYPOINT, "role": "main"}]
                      + [{"path": name, "role": "media"} for name in sorted(MEDIA_FILES)]),
    }
    (base / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return entrypoint


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="生成算子 #02 样例")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent),
                        help="样例输出目录（默认本目录）")
    args = parser.parse_args()
    entry = build_sample(Path(args.output))
    print(json.dumps({"entrypoint": str(entry), "lines": len(sample_lines())},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
