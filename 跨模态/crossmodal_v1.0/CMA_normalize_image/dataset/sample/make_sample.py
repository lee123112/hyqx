# [v4.2] 血缘输出已停用：本脚本生成的 lineage_history.json 仅为历史样例 fixture，
# 算子运行时不再读取/继承血缘；文件保留以维持样例目录结构不变。
"""生成算子 4 的样例素材（真实可解码媒体 + 覆盖各种分支的记录）。

运行::

    python dataset/sample/make_sample.py

产物（``dataset/sample/``）：
- ``media/``：Pillow 生成的真实 PNG/JPEG（含 EXIF 方向、RGBA、清晰/模糊对照），
  以及故意损坏/空文件的异常样例与占位音视频；
- ``demo.jsonl``：覆盖 正常、不适用透传、媒体缺失、解码失败、重复、类型非法 等分支；
- ``manifest.json``：``crossmodal-manifest-v1`` 入口清单；
- ``lineage_history.json``：历史阶段（S1 数据初筛）血缘，供 prior_lineage 使用；
- ``input_empty.jsonl`` / ``input_bad_encoding.jsonl``：空输入与非 UTF-8 输入。

除损坏/空文件外，所有图片都由 Pillow 真实生成并可被 Pillow 解码，不是伪造结果。
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps  # noqa: F401

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"demo-video-payload"
AUDIO_BYTES = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"demo-audio-payload"


def make_checkerboard(size: int, square: int = 8) -> Image.Image:
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, size, square):
        for x in range(0, size, square):
            if ((x // square) + (y // square)) % 2 == 0:
                draw.rectangle([x, y, x + square - 1, y + square - 1], fill=(0, 0, 0))
    return image


def write_media() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 超长边：1600x800 -> 期望等比缩放到 1024x512
    Image.new("RGB", (1600, 800), (200, 30, 30)).save(MEDIA_DIR / "wide_1600x800.png")

    # 2) 小图：64x32，allow_upscale=false 时保持原尺寸
    Image.new("RGB", (64, 32), (30, 60, 200)).save(MEDIA_DIR / "small_64x32.png")

    # 3) EXIF 方向 6（顺时针旋转 90°）：存储 320x160，显示应为 160x320
    rotated = Image.new("RGB", (320, 160), (20, 150, 90))
    ImageDraw.Draw(rotated).rectangle([0, 0, 159, 159], fill=(250, 220, 20))
    exif = Image.Exif()
    exif[274] = 6  # Orientation
    rotated.save(MEDIA_DIR / "exif_rot_90.jpg", format="JPEG", quality=92, exif=exif)

    # 4) 带 alpha 的 RGBA：合成到白色背景后应为不透明 RGB
    alpha = Image.new("RGBA", (64, 64), (255, 0, 0, 128))
    alpha.save(MEDIA_DIR / "alpha_64x64.png")

    # 5) 清晰 / 模糊对照：棋盘格与高斯模糊版本，Laplacian 方差应显著不同
    sharp = make_checkerboard(512, 8)
    sharp.save(MEDIA_DIR / "sharp_checker_512.png")
    sharp.filter(ImageFilter.GaussianBlur(radius=6)).save(MEDIA_DIR / "blur_checker_512.png")

    # 6) 异常样例：损坏内容与 0 字节
    (MEDIA_DIR / "corrupt.png").write_bytes(b"this-is-not-a-decodable-image")
    (MEDIA_DIR / "zero.png").write_bytes(b"")

    # 7) 音视频占位（用于不适用模态透传）
    (MEDIA_DIR / "vid_001.mp4").write_bytes(VIDEO_BYTES)
    (MEDIA_DIR / "aud_001.wav").write_bytes(AUDIO_BYTES)


def text_object(content: str, path: str = "03_text_cleaned.jsonl") -> dict:
    tokens = [token for token in content.split() if token]
    return {
        "path": path,
        "content": content,
        "char_count": len(content),
        "token_count": len(tokens),
        "tokens": tokens,
        "case_fold_applied": False,
    }


def record(sample_id: str, **overrides) -> dict:
    base = {
        "operator": "text_normalization",
        "operator_version": "v1.0.0",
        "uniqueId": "demoS2_f1",
        "stageId": "S2",
        "sample_id": sample_id,
        "source_language": "zh",
        "target_language": "en",
        "domain": "通用",
        "source": {"type": "web", "uri": "https://example.org/data", "license": "CC-BY-4.0"},
        "text": text_object("示例文本 example text %s" % sample_id),
        "image": None,
        "video": None,
        "audio": None,
        "alignment": {"source_to_text": "exact"},
        "qc": {"stage1_score": 0.91},
        "processing": {
            "01": {"status": "success"},
            "02": {"status": "success"},
            "03": {"status": "success", "metrics": {"token_count": 4}},
        },
        "metadata": {"batch": "demoS2", "collector": "fixture"},
    }
    base.update(overrides)
    return base


def image_ref(path: str) -> dict:
    return {
        "path": path,
        "width": 0,
        "height": 0,
        "format": None,
        "blur_score": None,
        "source": "upstream",
    }


def build_records() -> list:
    return [
        record("demo_04_wide_001", image=image_ref("media/wide_1600x800.png")),
        record("demo_04_small_001", image=image_ref("media/small_64x32.png")),
        record("demo_04_exif_001", image=image_ref("media/exif_rot_90.jpg"),
               text=text_object("旋转方向样例 rotated orientation sample")),
        record("demo_04_rgba_001", image=image_ref("media/alpha_64x64.png")),
        record("demo_04_sharp_001", image=image_ref("media/sharp_checker_512.png")),
        record("demo_04_blur_001", image=image_ref("media/blur_checker_512.png"),
               text=text_object("模糊样例 blur sample")),
        # 不适用模态：音视频组合 与 纯文本
        record("demo_04_av_001", text=None, image=None,
               video={"path": "media/vid_001.mp4", "fps": 25.0,
                      "duration_sec": 3.0, "frame_count": 75},
               audio={"path": "media/aud_001.wav", "sample_rate": 16000,
                      "channels": 1, "duration_sec": 3.0}),
        record("demo_04_text_001", text=text_object("纯文本无图像 text only, no image")),
        # 异常样例
        record("demo_04_missing_001", image=image_ref("media/missing.png")),
        record("demo_04_corrupt_001", image=image_ref("media/corrupt.png")),
        record("demo_04_zero_001", image=image_ref("media/zero.png")),
        record("demo_04_text_001"),          # 重复 sample_id
        record("demo_04_badtype_001", image=12),
    ]


def build_manifest() -> dict:
    media_files = sorted(
        str(path.relative_to(SAMPLE_DIR)).replace("\\", "/")
        for path in MEDIA_DIR.rglob("*") if path.is_file())
    artifacts = [{"path": "demo.jsonl", "role": "main"}]
    artifacts += [{"path": name, "role": "media"} for name in media_files]
    return {
        "format": "crossmodal-manifest-v1",
        "entrypoint": "demo.jsonl",
        "artifacts": artifacts,
    }


def build_lineage_history() -> list:
    return [{
        "operator": "data_screening",
        "operator_version": "v1.0.0",
        "uniqueId": "demoS1_f1",
        "stageId": "S1",
        "runId": "run001",
        "inputFile": "raw_demo.jsonl",
        "status": "success",
        "startedAt": "2026-01-01T09:00:00+08:00",
        "finishedAt": "2026-01-01T09:05:00+08:00",
        "config": {"business": {"min_text_chars": 2, "min_image_side": 32},
                   "deployment": {"stage_id": "S1"}},
        "statistics": {"total_lines": 15, "output": 14, "rejected": 1, "bad_lines": 0},
        "artifacts": [{"path": "01_screened.jsonl", "role": "main"}],
    }]


def main() -> None:
    write_media()
    lines = [json.dumps(item, ensure_ascii=False) for item in build_records()]
    # 第 9 行插入一条坏 JSON，用于验证坏行不中断且计入 bad_lines
    lines.insert(8, '{"sample_id": "demo_04_badjson_001", "image": ')
    (SAMPLE_DIR / "demo.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (SAMPLE_DIR / "manifest.json").write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    (SAMPLE_DIR / "lineage_history.json").write_text(
        json.dumps(build_lineage_history(), ensure_ascii=False, indent=2), encoding="utf-8")
    (SAMPLE_DIR / "input_empty.jsonl").write_bytes(b"")
    # 非法 UTF-8：0xFF 0xFE 是无效起始字节
    (SAMPLE_DIR / "input_bad_encoding.jsonl").write_bytes(
        '{"sample_id": "bad_encoding", "text": "'.encode("utf-8") + b"\xff\xfe" + b'"}\n')

    print("样例生成完成：")
    print("  demo.jsonl 行数 = %d" % (len(lines)))
    for name in sorted(path.name for path in MEDIA_DIR.iterdir()):
        path = MEDIA_DIR / name
        print("  media/%-24s %7d 字节" % (name, path.stat().st_size))
    for name in ("blur_checker_512.png", "sharp_checker_512.png"):
        with Image.open(MEDIA_DIR / name) as image:
            print("  %s size=%s mode=%s" % (name, image.size, image.mode))


if __name__ == "__main__":
    main()
