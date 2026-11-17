"""生成算子 22 的可重复样例数据（不下载任何外部数据）。

产物：
- ``dataset/sample/demo.jsonl``      主样例（覆盖生成 / 待处理 / 单独列出 / 隔离跳过 / 原始隔离 / 坏行）
- ``dataset/sample/manifest.json``   运行清单（声明入口为 ``demo.jsonl``）

运行：``python dataset/sample/make_sample.py``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SAMPLE_DIR = Path(__file__).resolve().parent
DEMO_PATH = SAMPLE_DIR / "demo.jsonl"
MANIFEST_PATH = SAMPLE_DIR / "manifest.json"


def _record(sample_id: str, description: str, modality_blocks: Dict[str, Any],
            version: str = "v1.0", batch_id: str = "batch-2026-10-A") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "sample_id": sample_id,
        "group_id": "g-%s" % sample_id.split("-")[1],
        "metadata": {"batch_id": batch_id, "version": version,
                     "source": "demo", "collected_at": "2026-09-20T09:00:00+08:00"},
        "text": {"content": description, "language": "zh"},
        "admission": {"license": "可训练"},
    }
    payload.update(modality_blocks)
    return payload


def _quality(status: str = "success", decision: str = "accepted",
             route: str = "image+text", label: str = "样本-质检通过") -> Dict[str, Any]:
    return {"processing": {"20": {"operator": "alignment_qc", "operator_number": 20,
                                  "operator_version": "v1.0.0", "stage_id": "S4",
                                  "status": status, "decision": decision,
                                  "action": "accept", "label": label,
                                  "destination": "质量报告待回写",
                                  "reason_code": "quality_check_passed", "route": route,
                                  "source_reference": "operator_20"}}}


def _candidate(status: str = "admitted") -> Dict[str, Any]:
    return {"processing": {"19": {"operator": "high_confidence_pool",
                                  "operator_number": 19, "status": status,
                                  "decision": "admit", "action": "keep",
                                  "label": "样本-评分：入池",
                                  "source_reference": "operator_19"}}}


def _isolated() -> Dict[str, Any]:
    return {"processing": {"21": {"operator": "mismatch_disposal",
                                  "operator_number": 21, "status": "isolated",
                                  "decision": "isolated", "action": "skip",
                                  "label": "样本-语义不通",
                                  "destination": "sample_isolation",
                                  "source_reference": "operator_21"}}}


def _merge(*parts: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge(merged[key], value)
            else:
                merged[key] = value
    return merged


def build_rows() -> List[Any]:
    rows: List[Any] = [
        # 1) 默认图文：生成（Token 以 <image> 开头）
        _merge(_record("demo-0001", "一只橘猫趴在绿色的草地上",
                       {"image": {"path": "media/demo-0001.png", "modality": "image"}}),
               _candidate(), _quality()),
        # 2) 图文 + flagged 质量结论：仍生成（旁路子集）
        _merge(_record("demo-0002", "穿红衣服的小女孩在放风筝",
                       {"image": {"path": "media/demo-0002.png", "modality": "image"}}),
               _candidate(), _quality("pending_review", "flagged",
                                      label="样本-质检：flagged 旁路")),
        # 3) 图文 + 局部标注证据（默认局部标注路线关闭 → 不产出该文件）
        _merge(_record("demo-0003", "左侧的白色帆船停靠在码头",
                       {"image": {"path": "media/demo-0003.png", "modality": "image"},
                        "local_annotations": {"operator_number": 17, "status": "confirmed",
                                              "confirmed_boxes": [{"box_id": "b1"}]}},
                       {"processing": {"17": {"operator": "local_to_whole_annotation",
                                              "status": "confirmed",
                                              "regions": [{"region_id": "b1"}]}}}),
               _candidate(), _quality()),
        # 4) 音文样本（默认音文路线关闭 → 待处理，不生成）
        _merge(_record("demo-0004", "这是一段关于天气的中文语音",
                       {"audio": {"path": "media/demo-0004.wav", "modality": "audio"}}),
               _candidate(), _quality(route="audio+text")),
        # 5) 视文样本（默认视文路线关闭 → 待处理，不生成）
        _merge(_record("demo-0005", "海边的日落延时摄影",
                       {"video": {"path": "media/demo-0005.mp4", "modality": "video"}}),
               _candidate(), _quality(route="video+text")),
        # 6) 还没有质量结论 → 不拦、留产线、不生成 Token
        _merge(_record("demo-0006", "尚未质检的城市夜景照片",
                       {"image": {"path": "media/demo-0006.png", "modality": "image"}}),
               _candidate()),
        # 7) 隔离区样本 → 不写进 samples.jsonl
        _merge(_record("demo-0007", "描述与图片明显不匹配的样本",
                       {"image": {"path": "media/demo-0007.png", "modality": "image"}}),
               _candidate(), _quality(), _isolated()),
        # 8) 不可训练 → 单独列出
        _merge(_record("demo-0008", "仅可用于评估的样例描述",
                       {"image": {"path": "media/demo-0008.png", "modality": "image"}}),
               _candidate(), _quality(),
               {"admission": {"license": "不可训练"}}),
        # 9) 缺文本 → 待补
        _merge(_record("demo-0009", "", {"image": {"path": "media/demo-0009.png",
                                                   "modality": "image"}}),
               _candidate(), _quality()),
        # 10) 非法媒体路径 → 原始隔离
        _merge(_record("demo-0010", "绝对路径引用，无法搬运",
                       {"image": {"path": "/abs/escape.png", "modality": "image"}}),
               _candidate(), _quality()),
        # 11) 坏行：JSON 不完整（原样字符串）
        '{"sample_id": "demo-0011", "text": ',
        # 12) 空行
        "",
    ]
    return rows


def write_sample() -> None:
    rows = build_rows()
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    with open(DEMO_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": DEMO_PATH.name,
                "artifacts": [{"path": DEMO_PATH.name, "role": "records"}],
                "note": "算子 22 样例：12 行输入（10 条记录 + 1 坏行 + 1 空行），无外网依赖"}
    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("已写出 %s（%d 行输入）" % (DEMO_PATH.name, len(rows)))
    print("已写出 %s" % MANIFEST_PATH.name)


if __name__ == "__main__":
    write_sample()
