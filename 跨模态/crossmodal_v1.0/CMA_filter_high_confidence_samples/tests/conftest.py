"""算子 19 测试公共设施：路径引导、配置夹具、上游记录构造器。

诚实边界：
- 本文件只构造**上游算子的字段结构**（#12–#18 命名空间，形状来自各算子
  ``dataset/DATA_SCHEMA.md`` 与源码），不加载任何模型、不联网、不下载数据；
- 算子 19 为**规则型**算子（表1 模型列 = ``--``），没有模型替身，
  全部断言都是确定性规则断言，不涉及任何精度宣称。
"""
from __future__ import annotations
import tempfile
import os

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BASE = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(BASE), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# 深合并与记录构造
# --------------------------------------------------------------------------- #

def deep_merge(*parts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按顺序深合并字典；后者覆盖前者的标量，字典逐键递归合并。"""
    merged: Dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
    return merged


def rec_12(tier: str = "auto_aligned", status: str = "scored",
           clip_score: Optional[float] = 0.42,
           reason: Optional[str] = None) -> Dict[str, Any]:
    """#12 图文评分命名空间（processing["12"] + 顶层 image_text_score）。"""
    node = {"operator": "#12", "status": status, "tier": tier,
            "label": "样本-评分：CLIP分", "reason": reason}
    return {"processing": {"12": node},
            "image_text_score": {"operator": "#12", "clip_score": clip_score,
                                 "score_semantics": "cosine", "tier": tier,
                                 "status": status, "reason": reason}}


def rec_13(passed: Optional[bool] = True, status: str = "passed",
           route_enabled: bool = True, reason: Optional[str] = None) -> Dict[str, Any]:
    """#13 音文评分命名空间。"""
    node = {"operator": "#13", "status": status, "route_enabled": route_enabled,
            "passed": passed, "reason": reason, "cosine_similarity": 0.51,
            "edit_distance_ratio": 0.12}
    return {"processing": {"13": node}}


def rec_14(passed: Optional[bool] = True, status: str = "passed",
           route_enabled: bool = True, reason: Optional[str] = None) -> Dict[str, Any]:
    """#14 视文评分命名空间（顶层 video_text_score 与处理节点同构）。"""
    node = {"operator": "#14", "status": status, "route_enabled": route_enabled,
            "passed": passed, "reason": reason, "video_text_score": 0.47,
            "clip_model": "CLIP-ViT/B-32"}
    return {"processing": {"14": node}, "video_text_score": node,
            "video_text_score_value": node.get("video_text_score")}


def rec_16(decision: str = "auto_aligned",
           human_veto_locked: bool = False) -> Dict[str, Any]:
    """#16 整体对齐结论命名空间。"""
    whole = {"decision": decision, "clip_score": 0.41,
             "confirmation_method": "auto_threshold",
             "human_verified": decision == "manual_aligned",
             "human_veto_locked": bool(human_veto_locked)}
    return {"alignment": {"whole": whole},
            "processing": {"16": {"operator": "16_whole_alignment",
                                  "status": decision,
                                  "label": "样本-评分：通过"}}}


def rec_15(status: str = "not_applicable", passed: Optional[bool] = None,
           reason_code: Optional[str] = "no_timestamp_timeline") -> Dict[str, Any]:
    """#15 时序结论命名空间（temporal_alignment + operator_15 + processing["15"]）。"""
    block = {"operator_number": 15, "operator_version": "v1.0.0",
             "status": status, "passed": passed, "reason_code": reason_code,
             "threshold_sec": 0.5, "max_delta_sec": 0.2,
             "action": "keep", "destination": "sample_pool",
             "human_verified": False, "review_required": True}
    return {"temporal_alignment": block,
            "operator_15": {"operator_number": 15, "status": status,
                            "decision": status, "reason_code": reason_code},
            "processing": {"15": {"operator": "temporal_consistency",
                                  "status": status, "decision": status,
                                  "reason_code": reason_code,
                                  "label": "流程-时序不适用"}}}


def rec_license(value: Optional[str] = "可训练",
                where: str = "admission") -> Dict[str, Any]:
    """授权结论（#10 归一化后写 ``admission.license``；原始值可在 ``metadata``）。"""
    if where == "admission":
        return {"admission": {"license": value}}
    if where == "metadata":
        return {"metadata": {"license": value}}
    if where == "authorization":
        return {"authorization": {"status": value}}
    if where == "top":
        return {"license": value}
    raise ValueError("未知的授权字段位置：%r" % where)


def rec_17(status: str = "pending_manual_review") -> Dict[str, Any]:
    """#17 局部-整体标注命名空间。"""
    return {"local_annotations": {"operator_number": 17, "status": status,
                                  "deliverable": status == "confirmed"},
            "processing": {"17": {"operator": "local_to_whole_annotation",
                                  "status": status,
                                  "reason_code": "route_disabled" if status == "skipped"
                                  else "manual_confirmation_required"}}}


def rec_18(status: str = "pending_manual") -> Dict[str, Any]:
    """#18 词-区域命名空间。"""
    return {"processing": {"18": {"operator": "phrase_region_annotation",
                                  "status": status, "route_enabled": status != "skipped",
                                  "prefilled": status == "prefilled",
                                  "reason": "route_disabled" if status == "skipped" else None}},
            "phrase_box_relations": []}


_UNSET = object()


def make_record(*parts: Any, sample_id: Any = _UNSET, **extra: Any) -> Dict[str, Any]:
    """构造一条完整输入记录。

    用法（两种都支持）：
    - ``make_record("hc-0001", rec_12(...), rec_16(...))``
    - ``make_record(rec_12(...), rec_16(...))``（sample_id 默认 ``hc-0001``）
    - ``make_record(None, rec_12(...))``（显式不写 sample_id）
    """
    if parts and (parts[0] is None or isinstance(parts[0], str)):
        if sample_id is _UNSET:
            sample_id = parts[0]
        parts = parts[1:]
    if sample_id is _UNSET:
        sample_id = "hc-0001"
    payload = deep_merge(*parts, extra)
    if sample_id is not None:
        payload = deep_merge({"sample_id": sample_id}, payload)
    return payload


def make_pair(pair_id: str = "pair-0001",
              left: Optional[Dict[str, Any]] = None,
              right: Optional[Dict[str, Any]] = None,
              **extra: Any) -> Dict[str, Any]:
    """构造 #11 风格的成对记录（pair_id + left/right.record）。"""
    payload: Dict[str, Any] = {"pair_id": pair_id, "group_id": "g-1",
                               "left": {"sample_id": "%s-left" % pair_id,
                                        "modalities": ["image"],
                                        "record": left or {}},
                               "right": {"sample_id": "%s-right" % pair_id,
                                         "modalities": ["text"],
                                         "record": right or {}}}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# 配置夹具
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_YAML = """\
operator:
  number: 19
  name: high_confidence_pool
  version: v1.0.0
stage_id: S3
enabled: true
required_alignment: auto|human
require_temporal_if_present: true
training_authorization: required
min_enabled_routes: 1
missing_upstream_policy: waiting_manual
"""


@pytest.fixture()
def config_file(tmp_path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


def tmp_path_yaml(base_config: Path, extra_yaml: str) -> Path:
    """基于默认配置再叠加一段 YAML 覆盖，用于配置边界测试。"""
    path = Path(base_config).with_name("config_variant_%d.yaml" % abs(hash(extra_yaml)))
    path.write_text(DEFAULT_CONFIG_YAML + extra_yaml, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_jsonl(path: Path, rows: List[Any]) -> Path:
    """写出 JSONL；字符串元素按原文写出（用于构造坏行）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
