"""幂等重建 #16 样例素材：候选对 JSONL + 人工回写事件 + 真实可打开 PNG 媒体。

样例只使用 Python 标准库（``zlib`` + ``struct`` 手写 PNG），不下载任何外部数据。
覆盖：阈值四个边界、抽检命中、无分数（上游隔离/未隔离）、人工三按钮、人工改描述（本机无
CLIP 权重时走显式延后）、人工否决锁定、不适用模态、坏行/缺字段/重复编号、非法与未匹配事件。

用法：
    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
ENTRY = SAMPLE_DIR / "demo.jsonl"
EVENTS = SAMPLE_DIR / "16_human_events.jsonl"
MANIFEST = SAMPLE_DIR / "manifest.json"

CLIP_MODEL = {"name": "CLIP-ViT/B-32", "version": "ViT-B-32", "library": "open_clip",
              "device": "cpu", "degraded": False, "real_backend": True,
              "score_semantics": "cosine"}
THRESHOLDS = {"isolation": 0.25, "auto_align": 0.32}


def write_png(path: Path, width: int, height: int, rgb: tuple) -> None:
    """手写最小合法 PNG（真彩色、无压缩），保证 media 可被真实解码。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def text_record(sample_id: str, content: str, batch_id: str, source_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "modalities": ["text"],
        "record": {
            "sample_id": sample_id,
            "source_id": source_id,
            "batch_id": batch_id,
            "text": {"content": content, "original": content, "lang": "zh",
                     "description_source": "caption"},
            "image": None,
            "video": None,
            "audio": None,
            "processing": {},
            "alignment": {"whole": None, "local": []},
            "qc": {},
            "metadata": {"batch_id": batch_id},
        },
    }


def image_record(sample_id: str, media_path: str, batch_id: str, source_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "modalities": ["image"],
        "record": {
            "sample_id": sample_id,
            "source_id": source_id,
            "batch_id": batch_id,
            "text": None,
            "image": {"path": media_path, "width": 32, "height": 24, "format": "PNG"},
            "video": None,
            "audio": None,
            "processing": {},
            "alignment": {"whole": None, "local": []},
            "qc": {},
            "metadata": {"batch_id": batch_id},
        },
    }


def pair(index: int, score, *, tier=None, sampling=False, model=CLIP_MODEL,
         veto_locked=False, media="media/cat.png", modality_pair=("text", "image"),
         upstream_absent=False, text=None, batch_id="batch-16",
         previous_whole=None) -> dict:
    """构造一个候选对；``upstream_absent=True`` 表示完全没有上游评分结果。"""
    left_id = "t%02d" % index if "text" in modality_pair else "a%02d" % index
    right_id = "i%02d" % index if "image" in modality_pair else "t%02d" % index
    content = text or ("样例描述 %02d：一只动物在户外场景中活动" % index)
    left = text_record(left_id, content, batch_id, "src-16")
    right = image_record(right_id, media, batch_id, "src-16")
    if modality_pair == ("audio", "text"):
        left = {"sample_id": right_id, "modalities": ["audio"], "record": {
            "sample_id": right_id, "batch_id": batch_id, "text": None, "image": None,
            "audio": {"path": "media/voice.wav", "duration": 1.0, "sample_rate": 16000},
            "video": None, "processing": {}, "alignment": {"whole": None, "local": []},
            "qc": {}, "metadata": {"batch_id": batch_id}}}
        right = text_record(left_id, content, batch_id, "src-16")
    item = {
        "pair_id": "pair-%s--%s" % (left.get("sample_id"), right.get("sample_id")),
        "group_id": "grp-src-src-16",
        "modality_pair": list(modality_pair),
        "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
                     "value": {"source_id": "src-16"}},
        "evidence_priority": 1,
        "left": left,
        "right": right,
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
                     "uniqueId": "sample_task"},
    }
    if not upstream_absent:
        score_node = {
            "clip_score": score,
            "score_semantics": "cosine" if score is not None else None,
            "tier": tier if tier is not None else (
                None if score is None else ("isolated" if score < 0.25
                                            else ("pending_manual" if score < 0.32
                                                  else "auto_aligned"))),
            "label": "样本-语义不通" if tier == "isolated" else "样本-评分：通过",
            "action": "isolate" if tier == "isolated" else "auto_align",
            "destination": "样本隔离库" if tier == "isolated" else "样本池",
            "status": "scored" if score is not None else "pending_model",
            "thresholds": THRESHOLDS,
            "model": model if score is not None else None,
        }
        item["image_text_score"] = score_node
    if sampling:
        item["metadata"] = {"sampling_hit": True, "sampling": {"hit": True, "layer": "layer-A"}}
    if veto_locked:
        item["alignment"] = {"whole": {
            "decision": "not_aligned", "clip_score": score if score is not None else 0.41,
            "confirmation_method": "human", "human_verified": True, "rescored": False,
            "human_veto_locked": True, "model": "CLIP-ViT/B-32", "model_version": "ViT-B-32",
            "reviewer_id": "ann-00", "reviewed_at": "2026-10-06T09:00:00+08:00",
            "threshold_review": 0.25, "threshold_auto": 0.32, "evidence": {}},
            "local": []}
    elif previous_whole is not None:
        item["alignment"] = {"whole": previous_whole, "local": []}
    return item


def build_records() -> list:
    """19 条物理行：15 条适用 + 1 条不适用 + 1 坏行 + 1 缺字段 + 1 重复编号。"""
    records = [
        # 1) >=0.32 → 自动对齐
        pair(1, 0.41),
        # 2) 高分但抽检命中 → 待人工
        pair(2, 0.55, sampling=True),
        # 3) 中间分 [0.25, 0.32) → 待人工
        pair(3, 0.28),
        # 4) 下边界 0.25（含）→ 待人工
        pair(4, 0.25),
        # 5) 上边界 0.32（含）→ 自动对齐
        pair(5, 0.32),
        # 6) 0.319999 → 待人工；同时引用缺失媒体（不计拒绝，只记 media_missing）
        pair(6, 0.319999, media="media/missing.png"),
        # 7) 0.24 → 沿用 #12 隔离（不对齐）
        pair(7, 0.24),
        # 8) 0.10 → 不对齐
        pair(8, 0.10),
        # 9) 无分数 + 上游 tier=isolated → 沿用隔离
        pair(9, None, tier="isolated", model=None),
        # 10) 无任何上游评分 → 待人工（不伪造分数）
        pair(10, None, upstream_absent=True),
        # 11) 人工结论"对齐"（事件 e1，分数 0.28）
        pair(11, 0.28, media="media/street.png"),
        # 12) 人工结论"不对齐"（事件 e2，分数 0.41）→ 锁定否决
        pair(12, 0.41),
        # 13) 人工结论"存疑"（事件 e3，分数 0.41）→ 交 #25
        pair(13, 0.41),
        # 14) 人工改写描述（事件 e4，无结论）→ 本机无权重时显式延后为待人工
        pair(14, 0.30, media="media/cat.png"),
        # 15) 上一轮已锁定人工否决（输入内已有 alignment.whole.human_veto_locked）
        pair(15, 0.55, veto_locked=True),
        # 16) 无图文组合（音文）→ 不适用，留产线不隔离
        pair(16, 0.90, modality_pair=("audio", "text"), upstream_absent=True),
        # 17) 坏行（JSON 语法错误）
        None,
        # 18) 缺 pair_id 也缺 sample_id → 原始隔离区
        {"left": {}, "right": {}},
        # 19) 重复 pair_id → 原始隔离区
        pair(1, 0.41),
    ]
    return records


def build_events() -> list:
    """7 条人工事件：4 条生效、1 条未匹配、1 条缺 reviewer_id、1 条被更晚事件覆盖。"""
    return [
        {"pair_id": "pair-t11--i11", "decision": "aligned", "reviewer_id": "ann-01",
         "reviewed_at": "2026-10-07T09:10:00+08:00",
         "review_task_id": "16-sample_task-pair-t11--i11",
         "previous_decision": "pending_manual_review",
         "comment": "人工确认图文整体一致"},
        {"pair_id": "pair-t12--i12", "decision": "不对齐", "reviewer_id": "ann-02",
         "reviewed_at": "2026-10-07T09:12:00+08:00",
         "previous_decision": "auto_aligned",
         "comment": "图文描述与图像内容明显不符"},
        {"pair_id": "pair-t13--i13", "decision": "存疑", "reviewer_id": "ann-03",
         "reviewed_at": "2026-10-07T09:20:00+08:00",
         "previous_decision": "auto_aligned",
         "comment": "需要在 #25 复核"},
        {"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
         "reviewed_at": "2026-10-07T09:30:00+08:00",
         "previous_decision": "pending_manual_review",
         "new_description": "样例描述 14（人工改写）：一只猫在室内沙发上休息",
         "comment": "原描述与图像不一致，已补充改写后的描述"},
        # 未匹配到任何输入记录 → 审计台账 target_not_found
        {"pair_id": "pair-t99--i99", "decision": "aligned", "reviewer_id": "ann-05",
         "reviewed_at": "2026-10-07T09:40:00+08:00"},
        # 缺 reviewer_id → 人工事件权限校验失败
        {"pair_id": "pair-t11--i11", "decision": "aligned",
         "reviewed_at": "2026-10-07T09:50:00+08:00"},
        # 与 e3 同键但 reviewed_at 更早 → 被覆盖（superseded）
        {"pair_id": "pair-t13--i13", "decision": "aligned", "reviewer_id": "ann-03",
         "reviewed_at": "2026-10-07T09:00:00+08:00"},
    ]


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    write_png(MEDIA_DIR / "cat.png", 32, 24, (200, 160, 120))
    write_png(MEDIA_DIR / "street.png", 32, 24, (120, 160, 200))

    lines = []
    for record in build_records():
        if record is None:  # 故意留下的坏行（JSON 语法错误）
            lines.append('{"pair_id": "pair-broken--i00", "left": {')
        else:
            lines.append(json.dumps(record, ensure_ascii=False))
    ENTRY.write_text("\n".join(lines) + "\n", encoding="utf-8")

    EVENTS.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in build_events()) + "\n",
        encoding="utf-8")

    MANIFEST.write_text(json.dumps({
        "format": "crossmodal-manifest-v1",
        "entrypoint": "demo.jsonl",
        "artifacts": [
            {"path": "demo.jsonl", "role": "main"},
            {"path": "16_human_events.jsonl", "role": "sidecar"},
            {"path": "media/cat.png", "role": "media"},
            {"path": "media/street.png", "role": "media"},
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("sample ready: %d lines -> %s" % (len(lines), ENTRY))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
