"""生成算子 25 的可复现样例（**不下载任何外部数据**）。

产出（同目录）::

    demo.jsonl            # 正常样例：28 条记录 + 5 行异常（空行/坏行/非对象/重复/缺标识）
    demo_pending.jsonl    # 未接入人工平台样例：队列结构一致但**不含任何复核结论**
    manifest.json         # crossmodal-manifest-v1 运行清单

设计说明
--------
- 记录只引用相对媒体路径；算子 25 **不读取媒体内容**（表1 模型列 ``--``），
  因此样例无需真实媒体文件；
- 结论按**固定清单**分配，不使用随机数、不重新抽样（表1 冻结：不重复 #24 抽样），
  从而稳定覆盖「通过 / 不通过 / 存疑 / 待人工」四条分支；
- 文本与结构完全确定，重复运行产出字节一致（回归测试会校验）。

用法::

    python dataset/sample/make_sample.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils import dispatch as dp  # noqa: E402  （仅依赖标准库）
from utils import transport  # noqa: E402  （仅依赖标准库）

SAMPLE_DIR = Path(__file__).resolve().parent
REVIEWED_AT = "2026-10-08T10:00:00+08:00"

BATCH_HYD = "batch-hyd-2026w41"
BATCH_SD = "batch-sd-2026w41"


def _base(sample_id: str, *, scene: str = "scene-hyd",
          modalities: Sequence[str] = ("text", "image"), batch: str = BATCH_HYD
          ) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "metadata": {"batch_id": batch, "source_id": "src-0001"},
        "scene_id": scene,
    }
    if "text" in modalities:
        record["text"] = {"content": "金毛犬在草地上接飞盘（%s）" % sample_id,
                          "language": "zh"}
    if "image" in modalities:
        record["image"] = {"path": "media/%s.png" % sample_id,
                           "width": 1280, "height": 720}
    if "audio" in modalities:
        record["audio"] = {"path": "media/%s.wav" % sample_id, "duration": 4.2}
    if "video" in modalities:
        record["video"] = {"path": "media/%s.mp4" % sample_id, "duration": 6.0,
                           "frames": [{"path": "media/%s-frame1.png" % sample_id,
                                       "timestamp": 0.0}]}
    return record


def _processing(record: Dict[str, Any], namespace: str, node: Dict[str, Any]
                ) -> Dict[str, Any]:
    processing = record.setdefault("processing", {})
    processing[namespace] = node
    return record


def _review(record: Dict[str, Any], verdict: str, *, reviewer: Optional[str] = "reviewer-01",
            round_no: int = 1, dimensions: Optional[Dict[str, str]] = None
            ) -> Dict[str, Any]:
    review: Dict[str, Any] = {"verdict": verdict, "reviewed_at": REVIEWED_AT,
                              "round": round_no, "source": "人工复核工作台"}
    if reviewer is not None:
        review["reviewer"] = reviewer
    if dimensions:
        review["dimensions"] = dict(dimensions)
    record["review"] = review
    return record


def uncertain(sample_id: str, *, source: str = "alignment", scene: str = "scene-hyd",
              modalities: Sequence[str] = ("text", "image")) -> Dict[str, Any]:
    if source == "alignment":
        record = _base(sample_id, scene=scene, modalities=modalities)
        record["alignment"] = {"type": "uncertain", "status": "uncertain",
                               "auto_aligned": False}
        return record
    if source == "16":
        record = _base(sample_id, scene=scene, modalities=modalities)
        record["alignment"] = {"type": "manual_aligned", "status": "manual_aligned",
                               "auto_aligned": False}
        return _processing(record, "16", {"status": "uncertain",
                                         "decision": "uncertain"})
    record = _base(sample_id, scene=scene, modalities=modalities)
    record["alignment"] = {"type": "manual_aligned", "status": "manual_aligned",
                           "auto_aligned": False}
    return _processing(record, "12", {"score": 0.28, "decision": "uncertain",
                                     "label": "样本-评分：待人工"})


def dispute(sample_id: str, *, decision: str = "uncertain", scene: str = "scene-hyd",
            modalities: Sequence[str] = ("text", "image")) -> Dict[str, Any]:
    record = _base(sample_id, scene=scene, modalities=modalities)
    record["alignment"] = {"type": "auto_aligned", "status": "auto_aligned",
                           "auto_aligned": True}
    return _processing(record, "24", {
        "decision": decision,
        "sampled": True,
        "role": "dispute",
        "label": "流程-抽检存疑" if decision == "uncertain" else "样本-语义不通",
        "action": "waiting_manual" if decision == "uncertain" else "isolate",
        "destination": "人工复核队列" if decision == "uncertain" else "样本隔离库",
    })


def returned(sample_id: str, *, retry_count: int = 1, scene: str = "scene-sd",
             modalities: Sequence[str] = ("text", "video")) -> Dict[str, Any]:
    record = _base(sample_id, scene=scene, modalities=modalities, batch=BATCH_SD)
    record["alignment"] = {"type": "manual_aligned", "status": "manual_aligned",
                           "auto_aligned": False}
    return _processing(record, "21", {
        "action": "return",
        "decision": "returned",
        "status": "returned",
        "label": "流程-退回重跑",
        "destination": "上游算子",
        "retry_count": int(retry_count),
    })


def passed(sample_id: str, *, scene: str = "scene-hyd",
           modalities: Sequence[str] = ("text", "image")) -> Dict[str, Any]:
    record = _base(sample_id, scene=scene, modalities=modalities)
    record["alignment"] = {"type": "auto_aligned", "status": "auto_aligned",
                           "auto_aligned": True}
    return _processing(record, "24", {
        "decision": "passed", "sampled": True, "role": "spot_check_sample",
        "label": "流程-抽检通过", "action": "keep", "destination": "交付候选",
    })


def auto_aligned_not_sampled(sample_id: str) -> Dict[str, Any]:
    record = _base(sample_id)
    record["alignment"] = {"type": "auto_aligned", "status": "auto_aligned",
                           "auto_aligned": True}
    _processing(record, "16", {"status": "uncertain"})
    return _processing(record, "24", {"decision": "passed", "sampled": False,
                                     "role": "not_sampled"})


def max_review(sample_id: str) -> Dict[str, Any]:
    record = uncertain(sample_id)
    record["review_count"] = 2
    record["review_history"] = [
        {"round": 1, "verdict": "pass", "reviewer": "reviewer-02"},
        {"round": 2, "verdict": "uncertain", "reviewer": "reviewer-05"},
    ]
    return record


def isolated(sample_id: str) -> Dict[str, Any]:
    record = _base(sample_id)
    record["alignment"] = {"type": "not_aligned", "status": "not_aligned",
                           "auto_aligned": False}
    return _processing(record, "21", {
        "isolated": True, "decision": "isolated", "action": "isolate",
        "label": "样本-语义不通", "destination": "样本隔离库",
    })


def build_records() -> List[Dict[str, Any]]:
    """28 条记录：三类样本 19 条 + 不适用 9 条（自动对齐未抽中 2、超限 2、
    抽检通过 3、上游隔离 2）。"""
    records: List[Dict[str, Any]] = []

    # 存疑（12 条）：覆盖通过 / 不通过 / 存疑 / 缺复核人 / 尚未复核
    records.append(_review(uncertain("unc-01", source="alignment"), "pass"))
    records.append(_review(uncertain("unc-02", source="alignment"), "fail",
                           dimensions={"semantics": "fail"}))
    records.append(_review(uncertain("unc-03", source="alignment"), "uncertain"))
    records.append(_review(uncertain("unc-04", source="alignment"), "pass",
                           reviewer=None))          # 缺复核人 → 待人工
    for index in range(5, 13):
        records.append(uncertain("unc-%02d" % index, source="alignment"))

    # 抽检异议（4 条）
    records.append(_review(dispute("dis-01", decision="uncertain"), "pass"))
    records.append(_review(dispute("dis-02", decision="rejected"), "fail",
                           dimensions={"semantics": "fail"}))
    records.append(dispute("dis-03", decision="uncertain"))
    records.append(_review(dispute("dis-04", decision="uncertain"), "uncertain"))

    # 加工退回（3 条）
    records.append(_review(returned("ret-01"), "pass"))
    records.append(returned("ret-02", retry_count=2))
    records.append(_review(returned("ret-03"), "fail",
                           dimensions={"semantics": "fail"}))

    # 不进队列：自动对齐且未被 #24 抽中 / 复核次数超限 / 抽检通过 / 上游隔离
    records.append(auto_aligned_not_sampled("aa-01"))
    records.append(auto_aligned_not_sampled("aa-02"))
    records.append(max_review("mr-01"))
    records.append(max_review("mr-02"))
    records.append(passed("pass-01"))
    records.append(passed("pass-02"))
    records.append(passed("pass-03"))
    records.append(isolated("iso-01"))
    records.append(isolated("iso-02"))
    return records


def strip_conclusions(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉全部人工结论：模拟「人工复核平台未接入」。"""
    result: List[Dict[str, Any]] = []
    for record in records:
        item = copy.deepcopy(record)
        item.pop("review", None)
        item.pop("human_review", None)
        item.pop("manual_review", None)
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            for key in ("review", "human_review", "manual_review"):
                metadata.pop(key, None)
        result.append(item)
    return result


def abnormal_lines(records: List[Dict[str, Any]]) -> List[str]:
    """空行 / 坏 JSON / 非对象 / 重复标识 / 缺标识。"""
    return [
        "",
        '{"sample_id": "unc-broken-01", "text": {"content": "截断的坏行"',
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


def main() -> int:
    records = build_records()
    demo_lines = [json.dumps(row, ensure_ascii=False) for row in records] \
        + abnormal_lines(records)
    write_lines(SAMPLE_DIR / "demo.jsonl", demo_lines)

    pending = strip_conclusions(records)
    write_lines(SAMPLE_DIR / "demo_pending.jsonl",
                [json.dumps(row, ensure_ascii=False) for row in pending])

    manifest = transport.build_manifest("demo.jsonl", [
        {"path": "demo.jsonl", "role": "review_queue_with_conclusions"},
        {"path": "demo_pending.jsonl", "role": "review_queue_without_conclusions"},
    ])
    transport.write_manifest(SAMPLE_DIR, manifest)

    categories: Dict[str, int] = {}
    for record in records:
        category, _evidence = dp.resolve_category(record)
        key = category or "not_eligible"
        categories[key] = categories.get(key, 0) + 1
    print("样例记录=%d 分类=%s" % (len(records), json.dumps(categories,
                                                        ensure_ascii=False,
                                                        sort_keys=True)))
    print("demo.jsonl 行数=%d（含 5 行异常，其中 1 行空行）" % len(demo_lines))
    print("demo_pending.jsonl 记录=%d（不含任何人工结论）" % len(pending))
    print("已写出 %s" % ", ".join(sorted(path.name
                                        for path in SAMPLE_DIR.glob("*.json*"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
