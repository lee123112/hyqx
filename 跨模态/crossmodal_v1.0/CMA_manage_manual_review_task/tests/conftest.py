"""算子 25「管理人工复核任务」测试夹具。

设计原则（与产线其它算子一致）：

- **不下载任何外部数据**：所有输入都在 ``tmp_path`` 内确定性构造，纯标准库；
- **业务参数字典与实现同源**：``biz()`` 直接调用
  ``utils.dispatch.default_business``，避免测试与实现各写一套默认值；
- **人工结论只作为输入证据**：夹具通过 ``review`` 对象写入结论，
  算子本身**不产生**任何人工结论（人工平台未接入时只能是待人工）。
"""
from __future__ import annotations
import tempfile
import os

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest

DEFAULT_SCENE = "scene-hyd"
DEFAULT_SOURCE = "src-001"
DEFAULT_BATCH = "batch-001"
REVIEWED_AT = "2026-10-08T10:00:00+08:00"

ENTRY_NAME = "25_review_candidates.jsonl"


# --------------------------------------------------------------------------- #
# 上游记录构造
# --------------------------------------------------------------------------- #

def base_record(sample_id: str, *, scene: Optional[str] = DEFAULT_SCENE,
                modalities: Sequence[str] = ("text", "image"),
                alignment: Optional[str] = None) -> Dict[str, Any]:
    """构造一条上游产物记录（模态引用只写相对路径，本算子不读媒体内容）。"""
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "metadata": {"batch_id": DEFAULT_BATCH, "source_id": DEFAULT_SOURCE},
    }
    if scene is not None:
        record["scene_id"] = scene
    if "text" in modalities:
        record["text"] = {"content": "金毛犬在草地上接飞盘（%s）" % sample_id,
                          "language": "zh"}
    if "image" in modalities:
        record["image"] = {"path": "media/%s.png" % sample_id}
    if "audio" in modalities:
        record["audio"] = {"path": "media/%s.wav" % sample_id}
    if "video" in modalities:
        record["video"] = {"path": "media/%s.mp4" % sample_id,
                           "frames": [{"path": "media/%s-frame1.png" % sample_id,
                                       "timestamp": 0.0}]}
    if alignment is not None:
        record["alignment"] = {"type": alignment, "status": alignment,
                               "auto_aligned": alignment == "auto_aligned"}
    return record


def set_processing(record: Dict[str, Any], namespace: str,
                   node: Dict[str, Any]) -> Dict[str, Any]:
    processing = record.get("processing")
    if not isinstance(processing, dict):
        processing = {}
        record["processing"] = processing
    processing[namespace] = node
    return record


def uncertain_record(sample_id: str, *, source: str = "alignment",
                     **kwargs: Any) -> Dict[str, Any]:
    """存疑样本：``alignment`` / ``#16`` / 低置信评分三条证据来源。"""
    if source == "alignment":
        return base_record(sample_id, alignment="uncertain", **kwargs)
    if source == "16":
        record = base_record(sample_id, alignment="manual_aligned", **kwargs)
        set_processing(record, "16", {"status": "uncertain",
                                     "decision": "uncertain"})
        return record
    if source == "low_confidence":
        record = base_record(sample_id, alignment="manual_aligned", **kwargs)
        set_processing(record, "12", {"decision": "uncertain", "score": 0.28,
                                      "label": "样本-评分：待人工"})
        return record
    raise ValueError("未知存疑来源：%s" % source)


def dispute_record(sample_id: str, *, decision: str = "uncertain",
                   sampled: bool = True, **kwargs: Any) -> Dict[str, Any]:
    """#24 抽检异议样本（判错或存疑），role=dispute。"""
    record = base_record(sample_id, alignment="auto_aligned", **kwargs)
    set_processing(record, "24", {
        "decision": decision,
        "sampled": sampled,
        "role": "dispute",
        "label": "流程-抽检存疑" if decision == "uncertain" else "样本-语义不通",
        "action": "waiting_manual" if decision == "uncertain" else "isolate",
        "destination": "人工复核队列" if decision == "uncertain" else "样本隔离库",
    })
    return record


def returned_record(sample_id: str, *, retry_count: int = 1,
                    **kwargs: Any) -> Dict[str, Any]:
    """加工退回样本：由 #21 登记 ``action=return`` / ``decision=returned``。"""
    record = base_record(sample_id, alignment="manual_aligned", **kwargs)
    set_processing(record, "21", {
        "action": "return",
        "decision": "returned",
        "status": "returned",
        "label": "流程-退回重跑",
        "destination": "上游算子",
        "retry_count": int(retry_count),
    })
    return record


def passed_record(sample_id: str, *, sampled: bool = True, **kwargs: Any) -> Dict[str, Any]:
    """#24 抽检通过、自动对齐的样本：三类之外，不生成复核任务。"""
    record = base_record(sample_id, alignment="auto_aligned", **kwargs)
    set_processing(record, "24", {
        "decision": "passed",
        "sampled": sampled,
        "role": "spot_check_sample",
        "label": "流程-抽检通过",
        "action": "keep",
        "destination": "交付候选",
    })
    return record


def isolated_record(sample_id: str, **kwargs: Any) -> Dict[str, Any]:
    """上游已隔离样本：保留但不进入本算子队列（not_applicable）。"""
    record = base_record(sample_id, alignment="not_aligned", **kwargs)
    set_processing(record, "21", {
        "isolated": True,
        "decision": "isolated",
        "action": "isolate",
        "label": "样本-语义不通",
        "destination": "样本隔离库",
    })
    return record


def auto_aligned_not_sampled_record(sample_id: str, **kwargs: Any) -> Dict[str, Any]:
    """自动对齐且**未被 #24 抽中**：即使带存疑标记也不进队列。"""
    record = base_record(sample_id, alignment="auto_aligned", **kwargs)
    set_processing(record, "16", {"status": "uncertain"})
    set_processing(record, "24", {"decision": "passed", "sampled": False,
                                  "role": "not_sampled"})
    return record


def with_rounds(record: Dict[str, Any], rounds: int) -> Dict[str, Any]:
    """登记历史复核轮数（用于「同一条最多复核 2 次」边界）。"""
    record["review_count"] = int(rounds)
    record["review_history"] = [
        {"round": index + 1, "verdict": "pass", "reviewer": "reviewer-01"}
        for index in range(int(rounds))
    ]
    return record


def with_review(record: Dict[str, Any], verdict: str, *, reviewer: Optional[str] = "reviewer-01",
                round_no: Optional[int] = None, dimensions: Optional[Dict[str, str]] = None,
                reviewed_at: str = REVIEWED_AT, source: str = "review") -> Dict[str, Any]:
    """写入人工复核结论（只作为输入证据）。"""
    review: Dict[str, Any] = {"verdict": verdict, "reviewed_at": reviewed_at}
    if reviewer is not None:
        review["reviewer"] = reviewer
    if round_no is not None:
        review["round"] = int(round_no)
    if dimensions:
        review["dimensions"] = dict(dimensions)
    record[source] = review
    return record


# --------------------------------------------------------------------------- #
# 输入目录装配
# --------------------------------------------------------------------------- #

def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """把记录（dict 或原始字符串行）写成 UTF-8 JSONL。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for item in rows:
        lines.append(item if isinstance(item, str)
                     else json.dumps(item, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_input_dir(root: Path, rows: Iterable[Any], *, unique_id: str = "prev_task",
                    entry_name: str = ENTRY_NAME) -> Path:
    """在 ``OUTPUT_DIR/<uniqueId>/input`` 下装配入口文件（含运行清单）。"""
    input_dir = Path(root) / unique_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    entry = write_jsonl(input_dir / entry_name, rows)
    from utils import transport

    manifest = transport.build_manifest(entry_name,
                                       [{"path": entry_name, "role": "review_queue"}])
    transport.write_manifest(input_dir, manifest)
    return entry


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not Path(path).is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def biz() -> Dict[str, Any]:
    """与部署配置同源的业务参数字典（默认**手动触发未开启**）。"""
    from utils import dispatch

    return dispatch.default_business()


@pytest.fixture()
def enabled_biz() -> Dict[str, Any]:
    """手动触发已开启的业务参数字典。"""
    from utils import dispatch

    return dispatch.default_business(enabled=True)


@pytest.fixture()
def mixed_rows() -> List[Dict[str, Any]]:
    """一次覆盖三类样本 + 非三类 + 上游隔离的确定性输入。"""
    rows: List[Dict[str, Any]] = [
        uncertain_record("q-unc-01"),
        uncertain_record("q-unc-02", source="16"),
        uncertain_record("q-unc-03", source="low_confidence"),
        dispute_record("q-dis-01", decision="uncertain"),
        dispute_record("q-dis-02", decision="rejected"),
        returned_record("q-ret-01", retry_count=1),
        passed_record("q-pass-01"),
        isolated_record("q-iso-01"),
    ]
    return rows


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
