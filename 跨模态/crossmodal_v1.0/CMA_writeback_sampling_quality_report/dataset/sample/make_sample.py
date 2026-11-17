"""生成算子 24 的可复现样例（**不下载任何外部数据**）。

产出（同目录）::

    demo.jsonl                  # 正常样例：40 条候选池 + 5 行异常（空行/坏行/非对象/重复/缺标识）
    demo_pending.jsonl          # 未接入人工平台样例：全部无复核结论 → 报告保持待回写
    20_quality_report.json      # 上游 #20 的「待回写质量报告」

设计说明
--------
- 记录只引用相对媒体路径；算子 24 **不读取媒体内容**，因此样例无需真实媒体文件；
- 复核结论按**确定性分层抽样的真实结果**分配（用 ``utils.strata.select_samples``
  以 ``sample_rate=0.20`` 计算），从而稳定覆盖「通过 / 判错 / 存疑 / 待人工」四条分支；
- 文本与结构完全确定，重复运行产出字节一致（回归测试会校验）。

用法::

    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils import strata as strata_mod  # noqa: E402  （仅依赖标准库）
from utils import transport  # noqa: E402  （仅依赖标准库）

SAMPLE_DIR = Path(__file__).resolve().parent
SALT = "operator24"
SAMPLE_RATE = 0.20

#: (场景, 模态, 对齐类型, 条数, sample_id 前缀)
STRATA_SPEC: Sequence[Tuple[Optional[str], Tuple[str, ...], Optional[str], int, str]] = (
    ("scene-hyd", ("text", "image"), "auto_aligned", 16, "sp-img"),
    ("scene-hyd", ("text", "audio"), "manual_aligned", 12, "sp-aud"),
    ("scene-sd", ("text", "video"), "auto_aligned", 8, "sp-vid"),
    (None, ("text", "image"), None, 4, "sp-unk"),
)

#: 按抽样顺序分配的人工复核结论：(verdict, reviewer, dimensions)
VERDICT_PLAN: Sequence[Tuple[str, Optional[str], Optional[Dict[str, str]]]] = (
    ("fail", "reviewer-07", {"semantics": "fail"}),
    ("pass", "reviewer-02", None),
    ("uncertain", "reviewer-11", {"temporal": "uncertain"}),
    ("pass", "reviewer-04", None),
    ("fail", "reviewer-09", {"semantics": "fail"}),
    ("pass", "reviewer-05", None),
    ("pass", "reviewer-03", None),
    ("pass", None, None),  # 缺失复核人身份 → 必须按待人工处理
)
REVIEWED_AT = "2026-10-07T10:00:00+08:00"


def build_record(sample_id: str, scene: Optional[str], modalities: Sequence[str],
                 alignment: Optional[str]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "metadata": {"batch_id": "batch-hyd-2026w40", "source_id": "src-0001"},
    }
    if scene is not None:
        record["scene_id"] = scene
    if "text" in modalities:
        record["text"] = {"content": "金毛犬在草地上接飞盘（%s）" % sample_id,
                          "language": "zh"}
    if "image" in modalities:
        record["image"] = {"path": "media/%s.png" % sample_id, "width": 1280,
                           "height": 720}
    if "audio" in modalities:
        record["audio"] = {"path": "media/%s.wav" % sample_id, "duration": 4.2}
    if "video" in modalities:
        record["video"] = {"path": "media/%s.mp4" % sample_id, "duration": 6.0,
                           "frames": [{"path": "media/%s-frame1.png" % sample_id,
                                       "timestamp": 0.0}]}
    if alignment is not None:
        record["alignment"] = {"type": alignment, "status": alignment,
                               "auto_aligned": alignment == "auto_aligned",
                               "confidence": 0.93 if alignment == "auto_aligned" else None}
    return record


def build_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for scene, modalities, alignment, count, prefix in STRATA_SPEC:
        for index in range(count):
            records.append(build_record("%s-%02d" % (prefix, index), scene,
                                        modalities, alignment))
    return records


def attach_reviews(records: List[Dict[str, Any]], rate: float) -> List[str]:
    """按确定性抽样结果写入复核结论，返回被赋结论的 sample_id 列表。"""
    items = []
    for record in records:
        key, _applicable = strata_mod.stratum_key(record)
        items.append({"sample_id": record["sample_id"], "stratum": key})
    selection = strata_mod.select_samples(items, rate=rate, salt=SALT)
    by_id = {record["sample_id"]: record for record in records}
    for position, sample_id in enumerate(selection["selected"]):
        verdict, reviewer, dimensions = VERDICT_PLAN[position % len(VERDICT_PLAN)]
        review: Dict[str, Any] = {"verdict": verdict,
                                  "reviewed_at": REVIEWED_AT,
                                  "source": "资深标注员抽检复核"}
        if reviewer is not None:
            review["reviewer"] = reviewer
        if dimensions:
            review["dimensions"] = dimensions
        by_id[sample_id]["review"] = review
    return list(selection["selected"])


def abnormal_lines(records: List[Dict[str, Any]]) -> List[str]:
    """空行 / 坏 JSON / 非对象 / 重复标识 / 缺标识。"""
    return [
        "",
        '{"sample_id": "sp-broken-01", "text": {"content": "截断的坏行"',
        "[1, 2, 3]",
        json.dumps(records[0], ensure_ascii=False),          # 重复 sample_id
        json.dumps({"scene_id": "scene-hyd", "text": {"content": "缺少 sample_id"}},
                   ensure_ascii=False),
    ]


def write_lines(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for line in lines:
            stream.write(line + "\n")


def json_lines(rows: Sequence[Dict[str, Any]]) -> List[str]:
    return [json.dumps(row, ensure_ascii=False) for row in rows]


def build_report(total: int) -> Dict[str, Any]:
    return {
        "format": "crossmodal-quality-report-v1",
        "report_id": "quality-report-prev_task-run001",
        "operator": "alignment_qc",
        "operator_number": 20,
        "operator_version": "v1.0.0",
        "stage_id": "S4",
        "uniqueId": "prev_task",
        "runId": "run001",
        "generated_at": "2026-10-07T15:08:48+08:00",
        "status": "waiting_writeback",
        "writeback": {
            "required": True,
            "writeback_by": "operator_24",
            "writeback_completed": False,
            "version_state": "pending_release",
        },
        "checks": {
            "text_nonempty": {"ok": total, "failed": 0},
            "modality_openable": {"ok": total, "failed": 0},
            "alignment_shape": {"ok": total, "failed": 0},
        },
        "metrics": {"alignment_confidence": {"avg": 0.93}, "text_length": {"avg": 18.4}},
        "statistics": {"input": total, "output": total, "origin_quarantined": 0},
        "note": "本样例模拟上游 #20 待回写报告，供算子 24 回写演示",
    }


def main() -> int:
    records = build_records()
    selected = attach_reviews(records, SAMPLE_RATE)

    demo_lines = json_lines(records) + abnormal_lines(records)
    write_lines(SAMPLE_DIR / "demo.jsonl", demo_lines)

    # 未接入人工平台时的同构样例：结构完全一致，但不含任何复核结论
    pending_records = build_records()
    write_lines(SAMPLE_DIR / "demo_pending.jsonl", json_lines(pending_records))

    report_path = SAMPLE_DIR / "20_quality_report.json"
    with open(report_path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(build_report(len(records)), stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    manifest = transport.build_manifest("demo.jsonl", [
        {"path": "demo.jsonl", "role": "candidate_pool"},
        {"path": "demo_pending.jsonl", "role": "candidate_pool_without_review"},
        {"path": "20_quality_report.json", "role": "upstream_quality_report"},
    ])
    transport.write_manifest(SAMPLE_DIR, manifest)

    print("候选池记录=%d 抽样(rate=%.2f)命中=%d" % (len(records), SAMPLE_RATE,
                                                   len(selected)))
    print("demo.jsonl 行数=%d（含 5 行异常）" % len(demo_lines))
    print("demo_pending.jsonl 记录=%d（无任何复核结论）" % len(pending_records))
    print("已写出 %s" % ", ".join(sorted(
        path.name for path in SAMPLE_DIR.glob("*.json*"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
