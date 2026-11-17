"""算子 22「生成跨模态Token」测试公共设施：路径引导、记录构造器、配置夹具。

诚实边界：
- 本文件只构造**上游算子的字段结构**（#19/#20/#21/#17/#18 命名空间，形状来自
  各算子 ``dataset/DATA_SCHEMA.md`` 与源码），不加载任何模型、不联网、不下载数据；
- 算子 22 为**系统功能型**（表1 模型列 = ``--``），没有模型替身，
  全部断言都是确定性规则断言，不涉及任何精度或模型分数宣称。
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


def rec_19(status: str = "admitted", decision: str = "admit") -> Dict[str, Any]:
    """#19 候选池命名空间（processing["19"] + operator_19）。"""
    node = {"operator": "high_confidence_pool", "operator_number": 19,
            "operator_version": "v1.0.0", "stage_id": "S3",
            "status": status, "decision": decision, "action": "keep",
            "destination": "candidate_pool", "reason_code": "all_gates_passed",
            "label": "样本-评分：入池", "source_reference": "operator_19"}
    return {"processing": {"19": node}, "operator_19": dict(node, sample_id="s")}


def rec_20(status: str = "success", decision: str = "accepted",
           label: str = "样本-质检通过", route: str = "image+text") -> Dict[str, Any]:
    """#20 质量结论命名空间（processing["20"]）。"""
    node = {"operator": "alignment_qc", "operator_number": 20,
            "operator_version": "v1.0.0", "stage_id": "S4",
            "status": status, "decision": decision, "action": "accept",
            "label": label, "destination": "质量报告待回写",
            "reason_code": "quality_check_passed", "route": route,
            "modalities": [item for item in route.split("+") if item],
            "source_reference": "operator_20"}
    return {"processing": {"20": node}}


def rec_21(status: str = "isolated", label: str = "样本-语义不通") -> Dict[str, Any]:
    """#21 隔离结果命名空间（processing["21"]）。"""
    node = {"operator": "mismatch_disposal", "operator_number": 21,
            "operator_version": "v1.0.0", "status": status, "decision": status,
            "label": label, "destination": "sample_isolation",
            "source_reference": "operator_21"}
    return {"processing": {"21": node}}


def rec_license(value: Optional[str] = "可训练", where: str = "admission") -> Dict[str, Any]:
    if where == "admission":
        return {"admission": {"license": value}}
    if where == "metadata":
        return {"metadata": {"license": value}}
    if where == "authorization":
        return {"authorization": {"status": value}}
    if where == "top":
        return {"license": value}
    raise ValueError("未知的授权字段位置：%r" % where)


def rec_text(content: Optional[str] = "一只猫趴在草地上",
             where: str = "object") -> Dict[str, Any]:
    if where == "object":
        return {"text": {"content": content, "language": "zh"}}
    if where == "string":
        return {"text": content}
    if where == "metadata":
        return {"metadata": {"description": content}}
    if where == "description":
        return {"description": content}
    raise ValueError("未知的文本字段位置：%r" % where)


def rec_image(path: Optional[str] = "media/cat.png") -> Dict[str, Any]:
    if path is None:
        return {}
    return {"image": {"path": path, "modality": "image"}}


def rec_audio(path: Optional[str] = "media/voice.wav") -> Dict[str, Any]:
    if path is None:
        return {}
    return {"audio": {"path": path, "modality": "audio"}}


def rec_video(path: Optional[str] = "media/clip.mp4") -> Dict[str, Any]:
    if path is None:
        return {}
    return {"video": {"path": path, "modality": "video"}}


def rec_17(status: str = "confirmed") -> Dict[str, Any]:
    return {"local_annotations": {"operator_number": 17, "status": status,
                                  "confirmed_boxes": [{"box_id": "b1"}]},
            "processing": {"17": {"operator": "local_to_whole_annotation",
                                  "status": status,
                                  "regions": [{"region_id": "b1"}]}}}


def rec_18(status: str = "prefilled") -> Dict[str, Any]:
    return {"processing": {"18": {"operator": "phrase_region_annotation",
                                  "status": status}},
            "phrase_box_relations": [{"phrase": "猫", "box_id": "b1"}]}


def rec_version(value: str = "v1.0") -> Dict[str, Any]:
    return {"metadata": {"version": value, "batch_id": "batch-A",
                         "source": "demo", "license": "可训练"}}


_UNSET = object()


def make_record(*parts: Any, sample_id: Any = _UNSET, **extra: Any) -> Dict[str, Any]:
    """构造一条完整输入记录。

    用法（两种都支持）：
    - ``make_record("tc-0001", rec_20(), rec_text(), rec_image())``
    - ``make_record(rec_20(), rec_text(), rec_image())``（sample_id 默认 ``tc-0001``）
    - ``make_record(None, rec_20())``（显式不写 sample_id）
    """
    if parts and (parts[0] is None or isinstance(parts[0], str)):
        if sample_id is _UNSET:
            sample_id = parts[0]
        parts = parts[1:]
    if sample_id is _UNSET:
        sample_id = "tc-0001"
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
  number: 22
  id: crossmodal_token
  name: crossmodal_token
  label: 生成跨模态Token
  version: v1.0.1
  enabled: true
transport:
  format: crossmodal-files-v1
  max_response_bytes: 67108864
stage_id: S5
enabled: true
exclude_isolated: true
samples_file_name: samples.jsonl
markers:
  image: "<image>"
  audio: "<audio>"
  video: "<video>"
routes:
  image_text: true
  audio_text: false
  video_text: false
  local_annotation: false
missing_quality_policy: waiting_manual
"""


ALL_ROUTES_CONFIG_YAML = DEFAULT_CONFIG_YAML.replace(
    "  audio_text: false\n  video_text: false\n  local_annotation: false\n",
    "  audio_text: true\n  video_text: true\n  local_annotation: true\n")


@pytest.fixture()
def config_file(tmp_path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def all_routes_config_file(tmp_path) -> Path:
    path = tmp_path / "config_all_routes.yaml"
    path.write_text(ALL_ROUTES_CONFIG_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_jsonl(path: Path, rows: List[Any], trailing_newline: bool = True) -> Path:
    """写出 JSONL；字符串元素按原文写出（用于构造坏行）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    text = "\n".join(lines) + ("\n" if trailing_newline else "")
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
