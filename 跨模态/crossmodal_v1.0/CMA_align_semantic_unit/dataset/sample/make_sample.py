"""生成算子 8 的本地 fixture（可独立开发、可重复生成）。

生成内容（见 dataset/DATA_SCHEMA.md）：

- ``demo.jsonl``：入口清单，5 条完整记录（post-06 形态）；
- ``README.md``：数据来源/生成方式/用途/校验结果；
- ``manifest.json``：``crossmodal-manifest-v1``，列出全部实际文件；
- ``03_rejects.jsonl``：上游旁路样本，用于验证上游处理结果被随包携带；
- ``04_images/``、``05_frames/``、``06_audio/``、``media/original/``：真实 PNG/WAV
  与占位 MP4 容器（仅 ftyp/mdat 盒，无轨道，只用于验证搬运与字节一致性）。

用法：``python dataset/sample/make_sample.py``（在本目录就地重新生成，保留脚本自身）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import wave
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

OPERATOR = "multimodal_preprocessing"
ENTRYPOINT = "demo.jsonl"
SAMPLE_ROOT = Path(__file__).resolve().parent
GENERATOR_NAME = "make_sample.py"
#: 由本脚本生成、可安全重建的路径（脚本自身不在此列）
GENERATED_DIRS = ("04_images", "05_frames", "06_audio", "media")
GENERATED_FILES = ("demo.jsonl", "manifest.json", "README.md", "03_rejects.jsonl")


# ------------------------------------------------------------------ 二进制构造

def write_png(path: Path, width: int, height: int, rgb: Tuple[int, int, int]) -> None:
    """写一个真实的纯色 PNG（尺寸与字节都可校验，用于尺寸/搬运验证）。"""
    raw = b"".join(
        b"\x00" + bytes(rgb) * width for _ in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_wav(path: Path, seconds: float, sample_rate: int = 16000) -> None:
    """写一个真实的可解析 WAV（16bit 单声道静音），时长与元数据一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(round(seconds * sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def write_mp4_placeholder(path: Path) -> None:
    """写占位 MP4 容器：只有 ftyp + mdat 盒，无轨道、不可解码。

    仅用于验证“原视频引用被搬运且字节一致”，不是可播放视频，也不冒充模型产物。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"07-fixture-original-video-placeholder"
    ftyp = struct.pack(">I", 20) + b"ftyp" + b"isom" + struct.pack(">I", 512) + b"isom"
    mdat = struct.pack(">I", len(payload) + 8) + b"mdat" + payload
    path.write_bytes(ftyp + mdat)


# ------------------------------------------------------------------ 记录构造

def tokenize(content: str) -> Tuple[List[str], List[List[int]]]:
    """确定性空白切分（record 级 fixture 数据，不是模型分词结果）。"""
    tokens: List[str] = []
    offsets: List[List[int]] = []
    start = 0
    for piece in content.split(" "):
        if piece:
            tokens.append(piece)
            offsets.append([start, start + len(piece)])
        start += len(piece) + 1
    return tokens, offsets


def text_object(sample_id: str, content: str, lang: str) -> Dict[str, Any]:
    tokens, offsets = tokenize(content)
    return {
        "content": content,
        "original": content,
        "tokens": tokens,
        "token_offsets": offsets,
        "lang": lang,
    }


def upstream_processing(where: str) -> Dict[str, Any]:
    return {
        "02": {"status": "success", "reason": None, "metrics": {"recorded": True}},
        "03": {"status": "success", "reason": None,
               "metrics": {"empty_after_clean": False}},
        "04": {"status": "success" if "image" in where else "not_applicable",
               "reason": None, "metrics": {"sharpness_pass": True}},
        "05": {"status": "success" if "video" in where else "not_applicable",
               "reason": None, "metrics": {"dropped_duplicates": 0}},
        "06": {"status": "success" if "audio" in where else "not_applicable",
               "reason": None, "metrics": {"transcoded": False}},
    }


def build_records() -> List[Dict[str, Any]]:
    beach = "Summer beach crowd walking past umbrellas."
    street = "Night market street with steam and neon signs."
    dance = "Dance rehearsal on the rooftop before sunset."

    records: List[Dict[str, Any]] = [
        # 1) text + image（成功单元，含原图引用）
        {
            "sample_id": "demo_07_a1_en",
            "text": text_object("demo_07_a1_en", beach, "en"),
            "image": {
                "path": "04_images/demo_07_a1_en.png",
                "original_path": "media/original/demo_07_a1_en.png",
                "width": 640, "height": 480, "format": "png", "blur_score": 128.5,
            },
            "video": None,
            "audio": None,
            "processing": upstream_processing("image"),
            "alignment": {"whole": None, "local": []},
            "qc": {"brightness_pass": True},
            "metadata": {"source": "fixture", "batch": "07-a"},
        },
        # 2) text + image（无 original_path，验证可选字段）
        {
            "sample_id": "demo_07_a2_en",
            "text": text_object("demo_07_a2_en", street, "en"),
            "image": {
                "path": "04_images/demo_07_a2_en.png",
                "width": 320, "height": 320, "format": "png", "blur_score": 96.0,
            },
            "video": None,
            "audio": None,
            "processing": upstream_processing("image"),
            "alignment": {"whole": {"offset": 0, "length": len(street)}, "local": []},
        },
        # 3) video + audio（text 为 null，video/audio 双模态单元）
        {
            "sample_id": "demo_07_v1_en",
            "text": None,
            "image": None,
            "video": {
                "path": "media/original/demo_07_v1.mp4",
                "duration": 12.0, "fps": 25.0,
                "frames": [
                    {"frame_index": 0, "timestamp": 0.0,
                     "path": "05_frames/demo_07_v1/frame_000000_000000000.png",
                     "width": 320, "height": 180},
                    {"frame_index": 1, "timestamp": 4.0,
                     "path": "05_frames/demo_07_v1/frame_000001_004000000.png",
                     "width": 320, "height": 180},
                    {"frame_index": 2, "timestamp": 8.0,
                     "path": "05_frames/demo_07_v1/frame_000002_008000000.png",
                     "width": 320, "height": 180},
                ],
            },
            "audio": {
                "path": "06_audio/demo_07_v1.wav",
                "original_path": "media/original/demo_07_v1.wav",
                "sample_rate": 16000, "channels": 1, "duration": 12.0,
                "segments": [
                    {"start": 0.0, "end": 6.0, "path": "06_audio/segments/demo_07_v1_0000.wav"},
                    {"start": 6.0, "end": 12.0, "path": "06_audio/segments/demo_07_v1_0001.wav"},
                ],
            },
            "processing": upstream_processing("video audio"),
            "qc": {"loudness_lufs": -23.0},
        },
        # 4) text only → not_applicable（合法但无跨模态组合，保留透传）
        {
            "sample_id": "demo_07_t1_zh",
            "text": text_object("demo_07_t1_zh", "屋顶排练舞蹈，日落前的光很暖。", "zh"),
            "image": None,
            "video": None,
            "audio": None,
            "processing": upstream_processing("text"),
        },
        # 5) video + audio 时长差 33%：v1.0.1 起 #8 不再做配对/时长比对，仅透传
        {
            "sample_id": "demo_07_v2_en",
            "text": text_object("demo_07_v2_en", dance, "en"),
            "image": {
                "path": "04_images/demo_07_v2_en.png",
                "width": 480, "height": 270, "format": "png", "blur_score": 110.0,
            },
            "video": {
                "path": "media/original/demo_07_v2.mp4",
                "duration": 12.0, "fps": 30.0,
                "frames": [
                    {"frame_index": 0, "timestamp": 0.0,
                     "path": "05_frames/demo_07_v2/frame_000000_000000000.png",
                     "width": 240, "height": 135},
                    {"frame_index": 1, "timestamp": 6.0,
                     "path": "05_frames/demo_07_v2/frame_000001_006000000.png",
                     "width": 240, "height": 135},
                ],
            },
            "audio": {
                "path": "06_audio/demo_07_v2.wav",
                "sample_rate": 16000, "channels": 2, "duration": 8.0,
                "segments": [],
            },
            "processing": upstream_processing("image video audio"),
        },
    ]
    return records


MEDIA_FILES: List[Tuple[str, str, str]] = [
    # (相对路径, 类型, 说明)
    ("04_images/demo_07_a1_en.png", "png", "640x480 图像"),
    ("media/original/demo_07_a1_en.png", "png", "a1 原图（保留）"),
    ("04_images/demo_07_a2_en.png", "png", "320x320 图像"),
    ("04_images/demo_07_v2_en.png", "png", "480x270 图像"),
    ("media/original/demo_07_v1.mp4", "mp4", "占位原视频容器"),
    ("media/original/demo_07_v2.mp4", "mp4", "占位原视频容器"),
    ("media/original/demo_07_v1.wav", "wav", "v1 原始音频 12.0s"),
    ("05_frames/demo_07_v1/frame_000000_000000000.png", "png", "关键帧 t=0.0"),
    ("05_frames/demo_07_v1/frame_000001_004000000.png", "png", "关键帧 t=4.0"),
    ("05_frames/demo_07_v1/frame_000002_008000000.png", "png", "关键帧 t=8.0"),
    ("05_frames/demo_07_v2/frame_000000_000000000.png", "png", "关键帧 t=0.0"),
    ("05_frames/demo_07_v2/frame_000001_006000000.png", "png", "关键帧 t=6.0"),
    ("06_audio/demo_07_v1.wav", "wav", "标准音频 12.0s/16kHz"),
    ("06_audio/segments/demo_07_v1_0000.wav", "wav", "片段 0.0-6.0s"),
    ("06_audio/segments/demo_07_v1_0001.wav", "wav", "片段 6.0-12.0s"),
    ("06_audio/demo_07_v2.wav", "wav", "标准音频 8.0s/16kHz"),
]

SIDECAR = "03_rejects.jsonl"

README_TEMPLATE = """# 算子 8 fixture 数据说明

## 数据来源
除 `make_sample.py` 自身外，本目录全部文件由 `dataset/sample/make_sample.py`
**程序化生成**（`input_source=fixture`），不来自生产语料，也未使用任何模型输出：

- `04_images/`、`05_frames/`、`06_audio/`：真实 PNG（纯色，尺寸与记录元数据一致）与
  真实 WAV（16bit 单声道静音，时长与记录一致）；
- `media/original/*.mp4`：占位容器（仅 `ftyp` + `mdat` 盒，无轨道、不可解码），
  只用于验证“原视频引用被搬运且字节一致”，不冒充真实视频，也不用于解码；
- `demo.jsonl`：入口清单，5 条 post-06 形态的完整记录；
- `03_rejects.jsonl`：上游旁路样本（1 条），用于验证上游处理结果随包携带；
- `manifest.json`：`crossmodal-manifest-v1`，entrypoint=`demo.jsonl`。

## 覆盖的判定分支（转写默认关闭）
| sample_id | 组合 | 预期 |
| --- | --- | --- |
| demo_07_a1_en | text+image（含原图） | `not_applicable`（`reason=no_audio_input`），`semantic_unit=null` |
| demo_07_a2_en | text+image（无 original_path） | `not_applicable` |
| demo_07_v1_en | video+audio（text=null） | `skipped`（未加载模型；打开转写后可转写） |
| demo_07_t1_zh | 仅 text | `not_applicable`（透传） |
| demo_07_v2_en | text+image+video+audio | `skipped`（不再做音视频时长比对） |

## 生成与校验
```powershell
python dataset/sample/make_sample.py
python -c "import sys; sys.path.insert(0,'.'); from utils import transport as t; print(t.load_manifest('dataset/sample')['entrypoint'])"
```

## 复现与限制
- 记录中的 `processing.02`—`processing.06` 是手工编写的上游形态样本，用于验证字段保留，
  不是真的跑过 02—06 的产物；
- 媒体内容为合成文件，只能验证流转与字节一致性，不能验证任何解码/特征质量。
"""


def build_manifest(media: List[str]) -> Dict[str, Any]:
    artifacts = [{"path": ENTRYPOINT, "role": "main"}]
    artifacts.extend({"path": path, "role": "media"} for path in media)
    artifacts.append({"path": SIDECAR, "role": "sidecar"})
    artifacts.append({"path": "README.md", "role": "sidecar"})
    artifacts.append({"path": GENERATOR_NAME, "role": "sidecar"})
    return {
        "format": "crossmodal-manifest-v1",
        "entrypoint": ENTRYPOINT,
        "artifacts": artifacts,
        "input_source": "fixture",
        "operator": OPERATOR,
        "notes": "fixture 由 dataset/make_sample.py 生成；占位 mp4 仅用于验证搬运",
    }


def generate(destination: Path = SAMPLE_ROOT) -> Dict[str, Any]:
    """就地重建 fixture；只清理本脚本生成过的路径，不删除脚本与其他文件。"""
    destination.mkdir(parents=True, exist_ok=True)
    for name in GENERATED_DIRS:
        shutil.rmtree(destination / name, ignore_errors=True)
    for name in GENERATED_FILES:
        target = destination / name
        if target.is_file():
            target.unlink()

    write_png(destination / "04_images/demo_07_a1_en.png", 640, 480, (32, 96, 160))
    write_png(destination / "media/original/demo_07_a1_en.png", 640, 480, (32, 96, 160))
    write_png(destination / "04_images/demo_07_a2_en.png", 320, 320, (160, 96, 32))
    write_png(destination / "04_images/demo_07_v2_en.png", 480, 270, (96, 160, 32))
    for name in ("demo_07_v1", "demo_07_v2"):
        write_mp4_placeholder(destination / ("media/original/%s.mp4" % name))
    write_wav(destination / "media/original/demo_07_v1.wav", 12.0)
    for index, stamp in ((0, "000000000"), (1, "004000000"), (2, "008000000")):
        write_png(destination / ("05_frames/demo_07_v1/frame_%06d_%s.png" % (index, stamp)),
                  320, 180, (40 + index * 30, 120, 200))
    for index, stamp in ((0, "000000000"), (1, "006000000")):
        write_png(destination / ("05_frames/demo_07_v2/frame_%06d_%s.png" % (index, stamp)),
                  240, 135, (200, 120, 40 + index * 30))
    write_wav(destination / "06_audio/demo_07_v1.wav", 12.0)
    write_wav(destination / "06_audio/segments/demo_07_v1_0000.wav", 6.0)
    write_wav(destination / "06_audio/segments/demo_07_v1_0001.wav", 6.0)
    write_wav(destination / "06_audio/demo_07_v2.wav", 8.0)

    records = build_records()
    with open(destination / ENTRYPOINT, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    sidecar = {
        "operator": "text_cleaning",
        "reason": "html_only_after_clean",
        "message": "清洗后仅剩标签，无有效文本",
        "sample_id": "demo_07_x1_en",
        "line": 4,
        "record": {"sample_id": "demo_07_x1_en", "text": {"content": ""}},
    }
    with open(destination / SIDECAR, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(sidecar, ensure_ascii=False) + "\n")

    (destination / "README.md").write_text(README_TEMPLATE, encoding="utf-8")

    media = [path for path, _kind, _note in MEDIA_FILES]
    manifest = build_manifest(media)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sizes = {path: (destination / path).stat().st_size for path in media}
    return {
        "destination": str(destination),
        "records": len(records),
        "mediaFiles": len(media),
        "mediaBytes": sum(sizes.values()),
        "entrypoint": ENTRYPOINT,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成算子 7 本地 fixture")
    parser.add_argument("--destination", default=str(SAMPLE_ROOT))
    args = parser.parse_args(argv)
    summary = generate(Path(args.destination))
    # 控制台可能是 GBK：用 ASCII 转义输出，保证任何终端都能解析
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
