"""算子 24「抽检并回写质量报告」测试夹具。

设计原则（与产线其它算子一致）：

- **不下载任何外部数据**：所有输入都在 ``tmp_path`` 内确定性构造，纯标准库；
- **业务参数字典与实现同源**：``business()`` 直接调用
  ``utils.spot_check.default_business``，避免测试与实现各写一套默认值；
- **人工复核结论只作为输入证据**：夹具通过 ``review`` 对象写入结论，
  算子本身**不产生**任何人工结论（未接入人工平台时只能是待人工）。
"""
from __future__ import annotations
import tempfile
import os

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest

#: 记录的模态引用只写相对路径；本算子不读媒体内容，因此样例无需真实媒体文件
DEFAULT_SCENE = "scene-hyd"
DEFAULT_SOURCE = "src-001"
DEFAULT_BATCH = "batch-001"


# --------------------------------------------------------------------------- #
# 记录与输入构造
# --------------------------------------------------------------------------- #

def make_record(sample_id: str, *, scene: Optional[str] = DEFAULT_SCENE,
                modalities: Sequence[str] = ("text", "image"),
                alignment: Optional[str] = "auto_aligned",
                verdict: Optional[str] = None,
                reviewer: Optional[str] = "reviewer-01",
                dimensions: Optional[Dict[str, str]] = None,
                reviewed_at: Optional[str] = "2026-10-07T10:00:00+08:00",
                review_source: str = "review",
                extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条候选池记录。

    ``alignment`` 同时写 ``alignment.type`` 与 ``alignment.status``，
    ``auto_aligned`` 由取值推导（与 #16 的字段命名保持一致）。
    """
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "metadata": {"batch_id": DEFAULT_BATCH, "source_id": DEFAULT_SOURCE},
    }
    if scene is not None:
        record["scene_id"] = scene
    if "text" in modalities:
        record["text"] = {"content": "金毛犬在草地上接飞盘 %s" % sample_id}
    if "image" in modalities:
        record["image"] = {"path": "media/%s.png" % sample_id}
    if "audio" in modalities:
        record["audio"] = {"path": "media/%s.wav" % sample_id}
    if "video" in modalities:
        record["video"] = {"path": "media/%s.mp4" % sample_id,
                           "frames": [{"path": "media/%s-frame1.png" % sample_id,
                                       "timestamp": 0.0}]}
    if alignment is not None:
        record["alignment"] = {
            "type": alignment,
            "status": alignment,
            "auto_aligned": alignment == "auto_aligned",
        }
    if verdict is not None or dimensions is not None:
        review: Dict[str, Any] = {}
        if verdict is not None:
            review["verdict"] = verdict
        if reviewer is not None:
            review["reviewer"] = reviewer
        if reviewed_at is not None:
            review["reviewed_at"] = reviewed_at
        if dimensions is not None:
            review["dimensions"] = dimensions
        record[review_source] = review
    if extra:
        record.update(extra)
    return record


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """把记录（dict 或原始字符串行）写成 UTF-8 JSONL。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for item in rows:
        if isinstance(item, str):
            lines.append(item)
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_quality_report(path: Path, *, unique_id: str = "prev_task",
                         status: str = "waiting_writeback",
                         total: int = 42) -> Path:
    """写一份与 #20 口径一致的「待回写质量报告」作为本算子的输入证据。"""
    payload = {
        "format": "crossmodal-quality-report-v1",
        "operator": "alignment_qc",
        "operator_number": 20,
        "operator_version": "v1.0.0",
        "stage_id": "S4",
        "uniqueId": unique_id,
        "runId": "run001",
        "generated_at": "2026-10-07T15:08:48+08:00",
        "status": status,
        "writeback": {
            "required": True,
            "writeback_by": "operator_24",
            "writeback_completed": False,
            "version_state": "pending_release",
        },
        "checks": {"text_nonempty": {"ok": total, "failed": 0}},
        "metrics": {"ppl": {"upstream": total}},
        "statistics": {"input": total, "output": total},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def build_input_dir(tmp_path: Path, rows: Iterable[Any], *,
                    entry_name: str = "24_candidate_pool.jsonl",
                    report: bool = True,
                    report_name: str = "20_quality_report.json",
                    unique_id: str = "prev_task") -> Path:
    """在 ``tmp_path`` 下组装一个「上游产物目录」，返回入口文件路径。"""
    entry = write_jsonl(tmp_path / entry_name, rows)
    if report:
        write_quality_report(tmp_path / report_name, unique_id=unique_id)
    return entry


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def biz() -> Dict[str, Any]:
    """与部署配置同源的业务参数字典。"""
    from utils import spot_check

    return spot_check.default_business()


@pytest.fixture()
def single_stratum(tmp_path: Path) -> Path:
    """**单层 10 条**输入：不抽检结论各不相同，用于稳定覆盖各判定分支。"""
    rows = [make_record("ss-%02d" % index) for index in range(10)]
    return build_input_dir(tmp_path, rows)


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
