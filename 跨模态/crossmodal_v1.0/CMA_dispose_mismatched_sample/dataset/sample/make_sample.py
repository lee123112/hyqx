"""生成算子 21 的可重复样例：``dataset/sample/demo.jsonl`` + ``media/`` + ``manifest.json``。

约束：
- 纯标准库（``zlib``/``struct``/``wave``），不联网、不下载大数据集；
- 无随机数：每次运行产出完全相同的字节，便于回归比对；
- 样例媒体全部本地合成，体积小（< 4KB 级）。

样例覆盖（与 ``dataset/DATA_SCHEMA.md`` §4 和 ``tests/test_operator.py::EXPECTED`` 对齐）：
放行 3 条、流程状态 2 条、不适用 1 条、待人工 2 条、原始隔离 2 条、
样本隔离 4 条（含锁定 2 条）、回流 1 条，另有 1 空行 + 3 坏行。
"""
from __future__ import annotations

import json
import math
import struct
import wave
import zlib
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
ENTRYPOINT = "demo.jsonl"
BATCH_ID = "BATCH-21-A"


def write_png(path: Path, width: int, height: int, rgb) -> None:
    """写一个最小合法 PNG（真彩色、无滤波）。"""
    rows = b""
    for _ in range(height):
        rows += b"\x00" + bytes(rgb) * width

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(rows, 9))
                     + chunk(b"IEND", b""))


def write_wav(path: Path, seconds: float = 0.1, rate: int = 16000) -> None:
    frames = int(seconds * rate)
    samples = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * index / rate)))
        for index in range(frames))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples)


def _entry(sample_id, **fields) -> dict:
    payload = {"sample_id": sample_id, "operator": "image_text_scoring",
               "stage": "S3", "event_time": "2026-10-07T09:00:00+08:00",
               "batch_id": BATCH_ID}
    payload.update(fields)
    return payload


def build_records() -> list:
    return [
        # 1) 评分分层：通过 → 放行
        _entry("s-pass-1", label="样本-评分：通过", action="keep", destination="样本池",
               record={"image": {"path": "media/pass_ok.png"}}),
        # 2) 评分分层：入池 → 放行
        _entry("s-pass-2", label="样本-评分：入池", action="keep", destination="样本池",
               record={"audio": {"path": "media/pass_ok.wav"}}),
        # 3) 评分分层：待人工 → 流程状态（人工平台未接入 → waiting_manual）
        _entry("s-proc-1", label="样本-评分：待人工", action="keep", destination="人工队列"),
        # 4) 流程状态：未配对 → 留产线，不进隔离区
        _entry("s-proc-2", label="流程-未配对", action="keep", destination="产线"),
        # 5) 无处置证据 → 不适用，留产线
        {"sample_id": "s-na-1", "batch_id": BATCH_ID},
        # 6) 原始文件问题 → 原始隔离区（媒体不得搬运）
        _entry("s-origin-1", label="原始文件-损坏", action="isolate", destination="原始隔离区",
               record={"image": {"path": "media/origin_broken.png"}}),
        # 7) 原始文件问题：编码异常 → 原始隔离区
        _entry("s-origin-2", label="原始文件-编码异常", action="isolate",
               destination="raw_isolation"),
        # 8) 业务问题：语义不通 → 样本隔离库（媒体不得搬运）
        _entry("s-sample-1", label="样本-语义不通", action="isolate", destination="样本隔离库",
               record={"image": {"path": "media/sample_bad.png"}}),
        # 9) 业务问题：敏感 → 样本隔离库
        _entry("s-sample-2", label="样本-敏感", action="isolate",
               destination="sample_isolation"),
        # 10) 回流超预算（retry_count=2 → 第 3 次）→ 锁定样本隔离库
        _entry("s-lock-1", action="return", destination="09_准备候选描述", retry_count=2,
               label="流程-待补描述"),
        # 11) 回流在预算内 → 回流上游
        _entry("s-ret-1", action="return", destination="09_准备候选描述", retry_count=0,
               label="流程-待补描述"),
        # 12) 本条已由 #21 退回 1 次 → 触顶锁定
        _entry("s-ret-2", action="return", destination="01_接入多模态批次", retry_count=1,
               label="流程-待补描述",
               record={"operator_21": {"return_count": 1}}),
        # 13) 需人工确认且平台未接入 → waiting_manual 降级
        _entry("s-manual-1", label="样本-评分：待人工", action="keep", destination="人工队列",
               requires_human_confirmation=True),
        # 14) 同源重复样本（与 s-pass-1 引用同一媒体）→ 仍提交下游，媒体只搬一次
        _entry("s-dup", label="样本-评分：通过", action="keep", destination="样本池",
               record={"image": {"path": "media/pass_ok.png"}}),
    ]


#: 坏行样例：保证与 demo.jsonl 的坏行位置/原因一致
BAD_LINES = (
    '{"sample_id": "broken-1", "label": "样本-语义不通"',   # 截断的 JSON → invalid_json
    "[1, 2, 3]",                                          # 非对象 → invalid_record
    '{"label": "样本-语义不通", "action": "isolate"}',      # 缺 sample_id
)


def build_manifest() -> dict:
    return {
        "format": "crossmodal-manifest-v1",
        "entrypoint": ENTRYPOINT,
        "artifacts": [
            {"path": "media/pass_ok.png", "role": "image"},
            {"path": "media/pass_ok.wav", "role": "audio"},
            {"path": "media/origin_broken.png", "role": "image"},
            {"path": "media/sample_bad.png", "role": "image"},
        ],
        "notes": "算子 21 样例：模拟上游标签/工序/动作/去向/时间/批次/重跑次数台账",
    }


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    write_png(MEDIA_DIR / "pass_ok.png", 8, 8, (32, 160, 96))
    write_wav(MEDIA_DIR / "pass_ok.wav")
    # 原始文件-损坏：故意写一个截断的 PNG 头
    (MEDIA_DIR / "origin_broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    write_png(MEDIA_DIR / "sample_bad.png", 8, 8, (200, 64, 64))

    lines = [json.dumps(record, ensure_ascii=False) for record in build_records()]
    lines.append("")                     # 空行（不计入坏行）
    lines.extend(BAD_LINES)
    (SAMPLE_DIR / ENTRYPOINT).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (SAMPLE_DIR / "manifest.json").write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    print("样例已生成：%s（%d 行）" % (SAMPLE_DIR / ENTRYPOINT, len(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
