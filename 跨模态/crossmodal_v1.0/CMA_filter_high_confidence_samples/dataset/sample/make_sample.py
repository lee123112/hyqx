"""生成算子 19 的可复现样例数据（无网络、无模型、无外部数据）。

运行::

    python dataset/sample/make_sample.py

产出（全部 UTF-8）：
- ``demo.jsonl``          主样例：覆盖四道必过项、两条路线组合、坏行、
                          非对象、空行、缺标识、重复标识、非法媒体路径
- ``demo_degraded.jsonl`` 全部记录必过项结论未定 → 运行状态 degraded
- ``demo_strict.jsonl``   必过项结论缺失 → 配合
                          ``missing_upstream_policy=failed`` 得到 failed
- ``manifest.json``       样例包清单（``crossmodal-manifest-v1``）

诚实边界：这些是**合成样例**，字段形状对齐上游 #12–#18 的 DATA_SCHEMA，
用于验证规则判定与守恒，不代表任何真实数据分布，也不含任何真实个人信息。
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 上游命名空间构造器（形状对齐 #10–#18，只取本算子读取的字段）
# --------------------------------------------------------------------------- #

def rec_10(license_value: Optional[str] = "可训练") -> Dict[str, Any]:
    payload: Dict[str, Any] = {"modalities": ["image", "text"],
                               "processing": {"10": {"operator": "input_admission",
                                                     "status": "passed"}}}
    if license_value is not None:
        payload["admission"] = {"license": license_value}
    return payload


def rec_12(tier: str = "auto_aligned", status: str = "scored",
           clip_score: float = 0.42) -> Dict[str, Any]:
    return {"processing": {"12": {"operator": "image_text_scoring",
                                  "status": status, "tier": tier,
                                  "label": "样本-评分：CLIP分", "reason": None}},
            "image_text_score": {"operator": "image_text_scoring",
                                 "clip_score": clip_score,
                                 "score_semantics": "cosine",
                                 "model_name": "ViT-B-32",
                                 "tier": tier, "status": status}}


def rec_13(passed: Optional[bool] = True, status: str = "passed",
           route_enabled: bool = True,
           reason: Optional[str] = None) -> Dict[str, Any]:
    return {"processing": {"13": {"operator": "audio_text_scoring",
                                  "status": status,
                                  "route_enabled": route_enabled,
                                  "passed": passed, "reason": reason,
                                  "cosine_similarity": 0.51,
                                  "edit_distance_ratio": 0.12}}}


def rec_14(passed: Optional[bool] = True, status: str = "passed",
           route_enabled: bool = True) -> Dict[str, Any]:
    node = {"operator": "video_text_scoring", "status": status,
            "route_enabled": route_enabled, "passed": passed,
            "video_text_score": 0.47, "clip_model": "CLIP-ViT/B-32"}
    return {"processing": {"14": node}, "video_text_score": node}


def rec_15(status: str = "not_applicable", passed: Optional[bool] = None,
           reason_code: Optional[str] = "no_timestamp_timeline") -> Dict[str, Any]:
    block = {"operator_number": 15, "operator_version": "v1.0.0",
             "status": status, "passed": passed, "reason_code": reason_code,
             "threshold_sec": 0.5, "max_delta_sec": 0.2,
             "action": "keep", "destination": "sample_pool"}
    return {"temporal_alignment": block,
            "processing": {"15": {"operator": "temporal_consistency",
                                  "status": status, "decision": status,
                                  "reason_code": reason_code,
                                  "label": "流程-时序不适用"}}}


def rec_16(decision: str = "auto_aligned") -> Dict[str, Any]:
    return {"alignment": {"whole": {"decision": decision, "clip_score": 0.41,
                                    "confirmation_method": "auto_threshold",
                                    "human_veto_locked": False}},
            "processing": {"16": {"operator": "whole_alignment",
                                  "status": decision,
                                  "label": "样本-评分：通过"}}}


def rec_17(status: str = "pending_manual_review") -> Dict[str, Any]:
    return {"local_annotations": {"operator_number": 17, "status": status},
            "processing": {"17": {"operator": "local_to_whole_annotation",
                                  "status": status,
                                  "reason_code": "manual_confirmation_required"}}}


def rec_18(status: str = "pending_manual") -> Dict[str, Any]:
    return {"processing": {"18": {"operator": "phrase_region_annotation",
                                  "status": status,
                                  "route_enabled": status != "skipped",
                                  "reason": None}},
            "phrase_box_relations": []}


def deep_merge(*parts: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        for key, value in (part or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
    return merged


def record(sample_id: Optional[str], *parts: Dict[str, Any]) -> Dict[str, Any]:
    payload = deep_merge(*parts)
    if sample_id is not None:
        payload = deep_merge({"sample_id": sample_id}, payload)
    return payload


# --------------------------------------------------------------------------- #
# 样例内容
# --------------------------------------------------------------------------- #

def build_demo() -> List[Any]:
    """主样例：19 条结构良好记录 + 2 行坏行 + 1 空行。"""
    rows: List[Any] = [
        # --- 入池（6 条）----------------------------------------------------- #
        # 图文路线 + 自动对齐 + 授权可训练 + 无时间轴（不适用）
        record("hc-0001", rec_10(), rec_12("auto_aligned", clip_score=0.44),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        # 音文路线 + 人工对齐
        record("hc-0002", rec_10(), rec_13(True, "passed"),
               rec_16("manual_aligned"), rec_15("not_applicable", None)),
        # 视文路线 + 存在时间轴且时序通过 + 声明媒体（用于验证媒体路径与搬运）
        record("hc-0003", rec_10(), rec_14(True, "passed"),
               rec_16("auto_aligned"), rec_15("passed", True, "within_threshold"),
               {"image_reference": {"path": "media/hc-0003.png"}}),
        # 局部标注仍待人工：**局部标注不作为入池硬条件**
        record("hc-0012", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None),
               rec_17("pending_manual_review"), rec_18("pending_manual")),
        # 成对记录（#11 结构）+ 双路线同时通过
        {"pair_id": "pair-0014", "group_id": "g-1",
         "left": {"sample_id": "hc-0014-text", "modalities": ["audio"],
                  "record": deep_merge(rec_10(), rec_13(True, "passed"))},
         "right": {"sample_id": "hc-0014-audio", "modalities": ["image", "text"],
                   "record": deep_merge(rec_12("auto_aligned"),
                                        rec_16("auto_aligned"),
                                        rec_15("not_applicable", None))}},
        # 时序路线关闭（不适用）+ 图文通过
        record("hc-0017", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None)),
        # --- 必过项失败 → 样本隔离库（6 条）--------------------------------- #
        record("hc-0004", rec_10(), rec_12("isolated", clip_score=0.11),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        record("hc-0005", rec_10(), rec_12("auto_aligned", clip_score=0.97),
               rec_16("not_aligned"), rec_15("passed", True)),
        record("hc-0006", rec_10("不可训练"), rec_12("auto_aligned"),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        record("hc-0007", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("failed", False, "max_delta_exceeded")),
        record("hc-0013", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None), rec_18("isolated")),
        record("hc-0016", rec_10(), rec_12("auto_aligned"), rec_13(False, "failed"),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        # --- 结论未定 → 待人工（4 条）--------------------------------------- #
        record("hc-0008", rec_10(), rec_12("pending_manual"),
               rec_16("pending_manual_review"), rec_15("not_applicable", None)),
        record("hc-0009", rec_10(), rec_16("auto_aligned"),
               rec_15("not_applicable", None)),
        record("hc-0010", rec_10(), rec_13(None, "pending", reason="missing_transcript"),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        record("hc-0011", rec_10(None), rec_12("auto_aligned"),
               rec_16("auto_aligned"), rec_15("not_applicable", None)),
        # --- 原始隔离（3 条：非法媒体路径 / 缺标识 / 重复标识）-------------- #
        record("hc-0015", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None), {"image": "/abs/escape.png"}),
        record(None, rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None)),
        record("hc-0017", rec_10(), rec_12("auto_aligned"), rec_16("auto_aligned"),
               rec_15("not_applicable", None)),
    ]
    rows.append('{"sample_id": "broken", ')
    rows.append("[1, 2, 3]")
    rows.append("")
    return rows


def build_degraded() -> List[Any]:
    """全部记录必过项结论未定 → 候选池为空、运行状态 degraded。"""
    return [record("dg-%04d" % index, rec_10(), rec_12("pending_manual"),
                   rec_16("pending_manual_review"),
                   rec_15("not_applicable", None))
            for index in range(1, 4)]


def build_strict() -> List[Any]:
    """必过项结论缺失 → 严格模式下 failed。"""
    return [record("st-0001", rec_10(), rec_16("auto_aligned"),
                   rec_15("not_applicable", None)),
            record("st-0002", rec_10(), rec_12("pending_manual"),
                   rec_16("uncertain"), rec_15("not_applicable", None))]


def write_text_lf(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def make_png(width: int = 16, height: int = 16,
             rgb: bytes = b"\x20\x60\xa0") -> bytes:
    """用 zlib/struct 生成一个合法的最小 PNG（确定性，无需外部数据）。"""
    raw = b"".join(b"\x00" + rgb * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR",
                         struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 9))
            + _png_chunk(b"IEND", b""))


def write_jsonl(path: Path, rows: List[Any]) -> None:
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    write_text_lf(path, "\n".join(lines) + "\n")


def main() -> int:
    write_jsonl(HERE / "demo.jsonl", build_demo())
    write_jsonl(HERE / "demo_degraded.jsonl", build_degraded())
    write_jsonl(HERE / "demo_strict.jsonl", build_strict())
    media_dir = HERE / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "hc-0003.png").write_bytes(make_png())
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
                "artifacts": [{"path": "demo.jsonl", "role": "main"},
                              {"path": "demo_degraded.jsonl", "role": "auxiliary"},
                              {"path": "demo_strict.jsonl", "role": "auxiliary"},
                              {"path": "media/hc-0003.png", "role": "media"},
                              {"path": "make_sample.py", "role": "generator"}]}
    write_text_lf(HERE / "manifest.json",
                  json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("已生成样例：demo.jsonl（主）/ demo_degraded.jsonl / demo_strict.jsonl "
          "/ media/hc-0003.png / manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
