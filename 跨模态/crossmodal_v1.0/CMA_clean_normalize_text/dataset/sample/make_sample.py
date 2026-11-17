# [v4.2] 血缘输出已停用：本脚本生成的 lineage_history.json 仅为历史样例 fixture，
# 算子运行时不再读取/继承血缘；文件保留以维持样例目录结构不变。
"""生成本算子自用的本地 fixture（不依赖上游算子产物）。

用法::

    python dataset/sample/make_sample.py                        # 只生成素材包
    python dataset/sample/make_sample.py --stage demoS2_f1 --output-dir output
    python dataset/sample/make_sample.py --stage demoS2_f1 --force

``--stage`` 会把素材包铺成任务目录结构 ``<output-dir>/<uniqueId>/input/``，
供 CLI 直接运行，也可作为 HTTP ``/predict`` 的输入素材。

素材内容全部为自造占位数据：图像是 1x1 PNG，视频是带 ftyp 的最小 mp4 容器，
音频是 44 字节 WAV 头，仅用于验证媒体引用可读与可搬运，不参与文本业务。
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
ENTRY = "demo.jsonl"

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
# 最小 mp4 容器（仅 ftyp box）
MP4_BYTES = (
    b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isomiso2"
    b"\x00\x00\x00\x08free"
)
# 44 字节 WAV 头（0 采样）
WAV_BYTES = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
    b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
    + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little")
)


def build_records():
    """返回 (记录行列表, 需要写出的原始 JSONL 文本)。"""
    records = [
        {
            "sample_id": "demo_03_img_001",
            "text": "<b>红色</b>方块&nbsp; 在桌面上 ",
            "image": "media/img_001.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "图文"},
            "processing": {"02": {"status": "success", "reason": None, "metrics": {"passed": 1}}},
        },
        {
            "sample_id": "demo_03_av_001",
            "text": None,
            "video": "media/vid_001.mp4",
            "audio": "media/aud_001.wav",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "音视频"},
            "processing": {"02": {"status": "success", "reason": None, "metrics": {"passed": 1}}},
        },
        {
            "sample_id": "demo_03_text_001",
            "text": "一只金毛犬在草地上接飞盘",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "纯文本"},
        },
        {
            "sample_id": "demo_03_dirty_001",
            "text": (
                "\uff21\uff22\uff23\u200b  \u0007 <p>\u4f60\u597d</p> \ufffd"
                "<script>alert(1)</script>\uff0c\u4e16\u754c \uff01"
            ),
            "image": "media/img_001.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "脏文本"},
        },
        {
            "sample_id": "demo_03_obj_001",
            "text": {"content": "\u5df2\u6709\u5185\u5bb9", "original": "\u5df2\u6709 \u539f\u59cb\u5185\u5bb9", "lang": "zh"},
            "image": "media/img_001.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "重跑"},
        },
        {
            "sample_id": "demo_03_av_002",
            "text": "   ",
            "video": "media/vid_001.mp4",
            "audio": "media/aud_001.wav",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "音视频"},
        },
        # 负例：清洗后为空且带图像 → 拒绝
        {
            "sample_id": "demo_03_empty_001",
            "text": "<p>   </p>",
            "image": "media/img_001.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "清空"},
        },
        # 负例：媒体引用缺失 → 拒绝（不伪造路径）
        {
            "sample_id": "demo_03_img_002",
            "text": "正常文本但媒体缺失",
            "image": "media/img_missing.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "缺媒体"},
        },
        # 负例：媒体文件损坏（0 字节） → 拒绝
        {
            "sample_id": "demo_03_img_003",
            "text": "正常文本但媒体损坏",
            "image": "media/img_zero.png",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "坏媒体"},
        },
        # 负例：重复 sample_id → 拒绝后出现的重复行
        {
            "sample_id": "demo_03_text_001",
            "text": "重复样本号",
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "重复"},
        },
        # 负例：text 类型非法
        {
            "sample_id": "demo_03_badtype_001",
            "text": 12,
            "metadata": {"source": "local_fixture", "license": "CC0", "domain": "非法类型"},
        },
        # 负例：缺 sample_id
        {"text": "缺少 sample_id 的记录"},
        # 负例：坏 JSON 行（下一行以原始文本方式写入）
    ]
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    lines.append('{"sample_id": "demo_03_badjson_001", "text": ')
    return records, lines


def write_media() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / "img_001.png").write_bytes(PNG_BYTES)
    (MEDIA_DIR / "vid_001.mp4").write_bytes(MP4_BYTES)
    (MEDIA_DIR / "aud_001.wav").write_bytes(WAV_BYTES)
    # 损坏媒体：0 字节，用于验证“不伪造文件、按 media_missing 拒绝”
    (MEDIA_DIR / "img_zero.png").write_bytes(b"")


def write_fixtures() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    write_media()
    _records, lines = build_records()
    (SAMPLE_DIR / ENTRY).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    manifest = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": ENTRY,
        "artifacts": [
            {"path": ENTRY, "role": "main"},
            {"path": "media/img_001.png", "role": "media"},
            {"path": "media/vid_001.mp4", "role": "media"},
            {"path": "media/aud_001.wav", "role": "media"},
            {"path": "media/img_zero.png", "role": "media"},
        ],
    }
    (SAMPLE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    # 严格 UTF-8 校验用负例（非法字节序列）
    (SAMPLE_DIR / "input_bad_encoding.jsonl").write_bytes(b'\xff\xfe{"sample_id": "x"}\n')
    # 空输入
    (SAMPLE_DIR / "input_empty.jsonl").write_bytes(b"")
    # 上游阶段血缘（S1），用于验证 prior_lineage 原样保留
    history = [{
        "operator": "data_screening",
        "operator_version": "v1.0.0",
        "uniqueId": "demoS2_f1",
        "stageId": "S1",
        "status": "success",
        "inputFile": "01_source.jsonl",
        "config": {"operator": {"name": "data_screening", "version": "v1.0.0"}},
        "statistics": {"total_lines": 13, "output": 8},
    }]
    (SAMPLE_DIR / "lineage_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def stage(unique_id: str, output_dir: str, force: bool = False) -> Path:
    """把素材包铺成 <output-dir>/<uniqueId>/input/ 任务结构。"""
    sys.path.insert(0, str(SAMPLE_DIR.parents[1]))
    from utils.io import task_dir, validate_unique_id

    validate_unique_id(unique_id)
    destination = task_dir(output_dir, unique_id) / "input"
    if destination.exists():
        if not force:
            raise SystemExit("目标已存在：%s（加 --force 覆盖）" % destination)
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE_DIR / ENTRY, destination / ENTRY)
    shutil.copyfile(SAMPLE_DIR / "manifest.json", destination / "manifest.json")
    shutil.copytree(MEDIA_DIR, destination / "media")
    shutil.copyfile(SAMPLE_DIR / "lineage_history.json", destination / "lineage_history.json")
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成算子 3 本地 fixture 并可铺成任务目录")
    parser.add_argument("--stage", metavar="UNIQUE_ID", default=None,
                        help="生成后铺成 <output-dir>/<uniqueId>/input/")
    parser.add_argument("--output-dir", default=str(SAMPLE_DIR.parents[1] / "output"),
                        help="任务输出根目录，默认本算子 output/")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 input 目录")
    args = parser.parse_args(argv)

    write_fixtures()
    print("已生成 fixture：%s" % SAMPLE_DIR)
    if args.stage:
        destination = stage(args.stage, args.output_dir, args.force)
        print("已铺成任务输入：%s" % destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
