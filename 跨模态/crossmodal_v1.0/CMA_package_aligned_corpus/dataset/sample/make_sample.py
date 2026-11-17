"""确定性生成算子 23 验收样例（不联网、不使用外部大数据）。

生成结构::

    dataset/sample/
      demo.jsonl                      # 材料齐全但质量报告/抽检未回写 → 待发布
      media/cm-0001.png ...           # 真实可解码媒体（zlib/struct/wave 合成）
      materials/quality_report.json   # #20/#24 质量报告（waiting_writeback）
      materials/sampling_conclusion.json
      materials/isolation_list.jsonl  # #21 隔离清单
      materials/version_notes.json    # 版本说明（声明启用路线 + 标注粒度）
      manifest.json                   # crossmodal-manifest-v1（entrypoint=demo.jsonl）
      deliverable/                    # 三项门禁全部满足 → 可交付
      incomplete/                     # 缺版本说明 → 仍待发布

覆盖的业务分支：
- 10 条可入包样本（图/文、音/文、视频/文、多模态、外部 OSS 引用、媒体缺失）；
- 3 条已在隔离区样本（2 条记录自带 #21 标记 + 1 条仅出现在隔离清单）；
- 3 条原始隔离（缺 sample_id、媒体路径绝对路径、Token 为空）；
- 2 条坏行（JSON 截断、JSON 数组）+ 1 行空行。
"""
from __future__ import annotations

import io
import json
import struct
import wave
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

BASE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 真实媒体合成
# --------------------------------------------------------------------------- #

def png_bytes(width: int = 12, height: int = 12, color: Sequence[int] = (210, 40, 40)) -> bytes:
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def wav_bytes(frames: int = 960, sample_rate: int = 16000, amplitude: int = 2600) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            payload += struct.pack("<h", amplitude if index % 2 == 0 else -amplitude)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 记录（#22 Token 样本清单形态）
# --------------------------------------------------------------------------- #

def token_record(sample_id: Any, *, route: str = "image_text",
                 modalities: Sequence[str] = ("image", "text"),
                 media: Sequence[str] = (), token: str = "<image> 一只橘猫坐在窗台上 <image>",
                 content: str = "一只橘猫坐在窗台上",
                 isolation: Dict[str, Any] | None = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "batch_id": "batch-A",
        "scene_id": "scene-01",
        "modalities": list(modalities),
        "routes": [route],
        "token": token,
        "text": {"content": content, "description_source": "caption"},
        "metadata": {"license": "可训练", "source": "batch-A", "retry_count": 0},
        "processing": {"22": {"operator": "token_builder", "status": "tokenized",
                              "route": route, "markers": ["<%s>" % modalities[0]]}},
        "qc": {"openable": True, "min_modalities": True},
    }
    if media:
        record["media"] = [{"path": item, "role": modalities[0]} for item in media]
    if isolation:
        record["processing"]["21"] = isolation
    return record


def isolation_marker(sample_id: str, *, reason_code: str = "score_below_threshold",
                     status: str = "isolated", label: str = "样本-语义不通"
                     ) -> Dict[str, Any]:
    return {"operator": "mismatch_disposal", "status": status, "isolated": True,
            "reason_code": reason_code, "label": label, "action": "isolate",
            "destination": "sample_isolation_library", "sample_id": sample_id,
            "event_time": "2026-10-07T18:00:00+08:00", "retry_count": 2}


RECORDS: Sequence[Any] = (
    token_record("cm-0001", media=["media/cm-0001.png"]),
    token_record("cm-0002", media=["media/cm-0002.png"], content="一只白猫趴在沙发上"),
    token_record("cm-0003", route="audio_text", modalities=("audio", "text"),
                 media=["media/cm-0003.wav"], token="<audio> 一段路况播报 <audio>",
                 content="一段路况播报"),
    token_record("cm-0004", route="video_text", modalities=("video", "text"),
                 media=["media/cm-0004.png"], token="<video> 一段街景 <video>",
                 content="一段街景"),
    # 记录声明了媒体相对路径，但该媒体不存在 → 只登记引用（不阻断入包）
    token_record("cm-0005", media=["media/cm-9999.png"], content="门前停着自行车"),
    # 多模态同场景样本（图 + 视频）
    token_record("cm-0006", modalities=("image", "video", "text"),
                 media=["media/cm-0001.png"],
                 token="<image> 猫 <image> <video> 同场景 <video>", content="猫与同场景视频"),
    # 外部 OSS 引用：只登记，不复制
    token_record("cm-0007", media=["oss://demo-bucket/tokens/cm-0007.png"],
                 content="OSS 引用样本"),
    token_record("cm-0008", media=["media/cm-0002.png"], content="白猫特写"),
    token_record("cm-0009", route="video_text", modalities=("video", "text"),
                 media=["media/cm-0004.png"], token="<video> 路口车流 <video>",
                 content="路口车流"),
    token_record("cm-0010", media=["media/cm-0001.png"], content="橘猫回眸"),
    # 已在隔离区（#21 标记两种写法）
    token_record("cm-0011", media=["media/cm-0002.png"], content="语义不通样本 A",
                 isolation=isolation_marker("cm-0011", status="isolated")),
    token_record("cm-0012", media=["media/cm-0002.png"], content="语义不通样本 B",
                 isolation=isolation_marker("cm-0012", status="quarantined",
                                            reason_code="semantic_mismatch")),
    # 仅出现在隔离清单中
    token_record("cm-0013", media=["media/cm-0002.png"], content="隔离清单样本"),
    # 原始隔离：缺标识
    {"batch_id": "batch-A", "modalities": ["image", "text"], "routes": ["image_text"],
     "token": "<image> 缺标识 <image>", "text": {"content": "缺标识"}},
    # 原始隔离：媒体引用为绝对路径
    token_record("cm-0015", media=["C:/temp/escape.png"], content="绝对路径引用"),
    # 原始隔离：Token 为空
    token_record("cm-0016", media=["media/cm-0001.png"], token="", content=""),
    # 坏行（JSON 截断 / 非对象）
    '{"sample_id": "cm-bad-1", ',
    '["not", "an", "object"]',
    "",
)

ISOLATION_LIST: Sequence[Dict[str, Any]] = (
    {"sample_id": "cm-0011", "label": "样本-语义不通", "stage": "S4",
     "operator": "mismatch_disposal", "action": "isolate",
     "destination": "sample_isolation_library", "reason_code": "score_below_threshold",
     "event_time": "2026-10-07T18:00:00+08:00", "batch_id": "batch-A", "retry_count": 2},
    {"sample_id": "cm-0012", "label": "样本-语义不通", "stage": "S4",
     "operator": "mismatch_disposal", "action": "isolate",
     "destination": "sample_isolation_library", "reason_code": "semantic_mismatch",
     "event_time": "2026-10-07T18:05:00+08:00", "batch_id": "batch-A", "retry_count": 2},
    {"sample_id": "cm-0013", "label": "样本-语义不通", "stage": "S4",
     "operator": "mismatch_disposal", "action": "isolate",
     "destination": "sample_isolation_library", "reason_code": "score_below_threshold",
     "event_time": "2026-10-07T18:10:00+08:00", "batch_id": "batch-A", "retry_count": 2},
)


def quality_report(*, written_back: bool) -> Dict[str, Any]:
    return {
        "format": "crossmodal-quality-report-v1",
        "operator": "alignment_qc",
        "operator_version": "v1.0.0",
        "report_id": "qr-batch-A-0001",
        "batch_id": "batch-A",
        "created_at": "2026-10-08T10:00:00+08:00",
        "checks": {"openable": True, "min_modalities": True, "blank_text": False},
        "statistics": {"input": 18, "valid": 16, "rejected": 2},
        "status": "written_back" if written_back else "waiting_writeback",
        "version_state": "deliverable" if written_back else "pending_release",
        "writeback": {"completed": written_back, "operator": "auto_review",
                      "completedAt": "2026-10-08T11:00:00+08:00" if written_back else None},
    }


def sampling_conclusion(*, written_back: bool) -> Dict[str, Any]:
    return {
        "format": "crossmodal-sampling-conclusion-v1",
        "operator": "auto_review",
        "batch_id": "batch-A",
        "sampling_rate": 0.1,
        "layers": {"image_text": 7, "audio_text": 1, "video_text": 2},
        "conclusion": {"judged_wrong": 1, "passed": 9, "isolated": 3},
        "status": "written_back" if written_back else "waiting_writeback",
        "writeback": {"completed": written_back,
                      "completedAt": "2026-10-08T11:30:00+08:00" if written_back else None},
    }


def version_notes() -> Dict[str, Any]:
    return {
        "format": "crossmodal-version-notes-v1",
        "dataset_version": "v1.0.0",
        "created_at": "2026-10-08",
        "enabled_routes": ["image_text", "audio_text", "video_text"],
        "annotation_granularity": "whole",
        "token_template": "<image>…<image> / <audio>…<audio> / <video>…<video>",
        "notes": ("启用路线与标注粒度由本文件声明；交付状态由 23 号算子按"
                  "『训练文件已生成 + 质量报告已写回 + 抽检已写回』三项只读门禁判定。"),
    }


# --------------------------------------------------------------------------- #
# 写出
# --------------------------------------------------------------------------- #

def _write_lines(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _manifest(entrypoint: str, materials: Sequence[str], media: Sequence[str]
              ) -> Dict[str, Any]:
    return {"format": "crossmodal-manifest-v1", "entrypoint": entrypoint,
            "artifacts": [{"path": entrypoint, "role": "records"}]
            + [{"path": "materials/%s" % item, "role": "materials"} for item in materials]
            + [{"path": item, "role": "media"} for item in media]}


def build_sample(base: Path = BASE) -> List[str]:
    """生成全部样例，返回写出的相对路径列表。"""
    base = Path(base)
    written: List[str] = []

    media_files = {"media/cm-0001.png": png_bytes(color=(210, 40, 40)),
                   "media/cm-0002.png": png_bytes(color=(40, 90, 210)),
                   "media/cm-0003.wav": wav_bytes(),
                   "media/cm-0004.png": png_bytes(width=16, height=9,
                                                  color=(30, 160, 90))}
    for relative, payload in media_files.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written.append(relative)

    # 场景 1：材料齐全但质量报告/抽检未回写 → 待发布
    _write_lines(base / "demo.jsonl", RECORDS)
    written.append("demo.jsonl")
    _write_json(base / "materials" / "quality_report.json",
                quality_report(written_back=False))
    _write_json(base / "materials" / "sampling_conclusion.json",
                sampling_conclusion(written_back=False))
    _write_lines(base / "materials" / "isolation_list.jsonl", ISOLATION_LIST)
    _write_json(base / "materials" / "version_notes.json", version_notes())
    written.extend(["materials/quality_report.json", "materials/sampling_conclusion.json",
                    "materials/isolation_list.jsonl", "materials/version_notes.json"])
    _write_json(base / "manifest.json",
                _manifest("demo.jsonl",
                          ["quality_report.json", "sampling_conclusion.json",
                           "isolation_list.jsonl", "version_notes.json"],
                          sorted(media_files)))
    written.append("manifest.json")

    # 场景 2：三项门禁全部满足 → 可交付
    deliverable = base / "deliverable"
    _write_lines(deliverable / "demo.jsonl", RECORDS)
    _write_json(deliverable / "materials" / "quality_report.json",
                quality_report(written_back=True))
    _write_json(deliverable / "materials" / "sampling_conclusion.json",
                sampling_conclusion(written_back=True))
    _write_lines(deliverable / "materials" / "isolation_list.jsonl", ISOLATION_LIST)
    _write_json(deliverable / "materials" / "version_notes.json", version_notes())
    _write_json(deliverable / "manifest.json",
                _manifest("demo.jsonl",
                          ["quality_report.json", "sampling_conclusion.json",
                           "isolation_list.jsonl", "version_notes.json"], []))
    written.append("deliverable/demo.jsonl")

    # 场景 3：已回写但缺版本说明 → 仍待发布
    incomplete = base / "incomplete"
    _write_lines(incomplete / "demo.jsonl", RECORDS)
    _write_json(incomplete / "materials" / "quality_report.json",
                quality_report(written_back=True))
    _write_json(incomplete / "materials" / "sampling_conclusion.json",
                sampling_conclusion(written_back=True))
    _write_lines(incomplete / "materials" / "isolation_list.jsonl", ISOLATION_LIST)
    _write_json(incomplete / "manifest.json",
                _manifest("demo.jsonl",
                          ["quality_report.json", "sampling_conclusion.json",
                           "isolation_list.jsonl"], []))
    written.append("incomplete/demo.jsonl")

    # 子场景同样携带真实媒体，保证它们的交付包也能完整复现
    for scenario in (deliverable, incomplete):
        for relative, payload in media_files.items():
            target = scenario / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    return written


def main() -> int:
    written = build_sample(BASE)
    print("已生成 %d 个样例文件：" % len(written))
    for item in written:
        print("  - %s" % item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
