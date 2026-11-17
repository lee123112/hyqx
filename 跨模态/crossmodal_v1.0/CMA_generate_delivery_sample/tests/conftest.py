"""算子 26 测试公共设施：路径引导、#23 交付包夹具、上游记录夹具。

诚实边界：
- 本文件只构造**上游算子的字段结构**（#22 Token 样本 + #23 交付包 manifest/version/
  materials + #12/#13/#14 评分结论 + #16 标注结论 + #21 隔离标记），形状来自
  `跨模态数据流说明_2026-10-06.md`、本目录 `DEVELOPMENT_PLAN.md` 与同项目已完成算子的
  `dataset/DATA_SCHEMA.md`；不加载任何模型、不联网、不下载外部数据；
- 算子 26 为**系统功能型**算子（表1 模型列 = ``--``），没有模型替身，全部断言都是
  确定性规则断言，不涉及任何精度宣称；
- 全部输入均为纯文本 JSON/JSONL，样例不依赖任何媒体二进制，可重复生成固定字节。
"""
from __future__ import annotations
import tempfile
import os

import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest

BASE = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(BASE), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# 上游记录构造（#22 Token 记录 + #23 交付标记 + #12/#16 结论）
# --------------------------------------------------------------------------- #

def score_block(route: str = "image_text", *, value: float = 0.41,
                tier: str = "auto_aligned", label: str = "样本-评分：通过",
                operator_number: int = 12, model: str = "CLIP-ViT/B-32") -> Dict[str, Any]:
    """#12 / #13 / #14 评分结论块（只构造结构，不产生任何真实分数语义）。"""
    names = {12: "image_text_scoring", 13: "audio_text_scoring", 14: "video_text_scoring"}
    return {"operator": names[operator_number], "operator_number": operator_number,
            "operator_version": "v1.0.0", "status": "scored", "decision": tier,
            "label": label, "score": value, "tier": tier, "route": route,
            "thresholds": {"isolation": 0.25, "auto_align": 0.32},
            "model": {"name": model, "version": "ViT-B-32", "degraded": False}}


def annotation_block(*, conclusion: str = "对齐", decision: str = "aligned",
                     human_verified: bool = False,
                     confirmation_method: str = "auto") -> Dict[str, Any]:
    """#16 整体对齐标注结论块。"""
    return {"operator": "whole_alignment", "operator_number": 16,
            "operator_version": "v1.0.0", "status": "aligned", "decision": decision,
            "conclusion": conclusion, "confirmation_method": confirmation_method,
            "human_verified": human_verified, "editor": "auto"}


def isolation_marker(sample_id: str = "cm-9911",
                     *, status: str = "isolated") -> Dict[str, Any]:
    """#21 处置错配样本写入的隔离标记（不得被本算子抽样顶替）。"""
    return {"operator": "mismatch_disposal", "operator_number": 21, "status": status,
            "decision": "isolated", "isolated": True, "action": "isolate",
            "label": "样本-语义不通", "destination": "sample_isolation_library",
            "reason_code": "score_below_threshold", "sample_id": sample_id,
            "event_time": "2026-10-08T10:00:00+08:00", "retry_count": 2}


def sample_record(sample_id: Optional[str] = "cm-0001", *,
                  route: str = "image_text",
                  modalities: Optional[Sequence[str]] = None,
                  description: Optional[str] = "一只橘猫坐在窗台上",
                  annotation: Optional[Dict[str, Any]] = None,
                  score: Optional[Dict[str, Any]] = None,
                  deliverable: bool = True,
                  isolation: Optional[Dict[str, Any]] = None,
                  media: Optional[Sequence[str]] = None,
                  batch_id: str = "batch-A",
                  license_value: str = "可训练",
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条「#22 Token + #23 已入交付包」形态的记录。"""
    modal = list(modalities) if modalities is not None else _default_modalities(route)
    processing: Dict[str, Any] = {
        "22": {"operator": "crossmodal_token", "operator_number": 22,
               "status": "token_generated", "route": route, "routes": [route],
               "route_files": ["samples_%s.jsonl" % route],
               "markers": _default_markers(route)},
        "23": {"operator": "delivery_package", "operator_number": 23,
               "status": "packaged",
               "decision": "deliverable" if deliverable else "pending_release",
               "member": "train/samples.jsonl", "route": route,
               "source_reference": "operator_23"},
    }
    if score is not None:
        number = int(score.get("operator_number") or 12)
        processing[str(number)] = score
    if annotation is not None:
        processing["16"] = annotation
    if isolation is not None:
        processing["21"] = isolation

    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "group_id": "grp-%s" % (sample_id or "none"),
        "modalities": modal,
        "routes": [route],
        "route": route,
        "admission": {"license": license_value},
        "metadata": {"batch_id": batch_id, "source": "demo", "retry_count": 0,
                     "collected_at": "2026-10-01T09:00:00+08:00"},
        "text": {"content": description, "language": "zh",
                 "description_source": "caption"},
        "processing": processing,
        "operator_22": {"operator": "crossmodal_token", "operator_number": 22,
                        "operator_version": "v1.0.0", "status": "token_generated",
                        "route": route, "routes": [route],
                        "token_text": "<%s> %s" % (route.split("_")[0],
                                                   description or ""),
                        "modalities": modal, "sample_id": sample_id},
        "operator_23": {"operator": "delivery_package", "operator_number": 23,
                        "operator_version": "v1.0.0",
                        "status": "packaged",
                        "decision": "deliverable" if deliverable else "pending_release",
                        "member": "train/samples.jsonl", "sample_id": sample_id,
                        "route": route},
    }
    record[route.split("_")[0]] = {"path": "media/%s.bin" % (sample_id or "x"),
                                   "modality": route.split("_")[0]}
    if media is not None:
        record["media"] = [{"path": item, "role": "media"} for item in media]
    if annotation is not None:
        record["alignment"] = {"whole": dict(annotation)}
    if score is not None and route == "image_text":
        record["image_text_score"] = dict(score)
    if extra:
        record = deep_merge(record, extra)
    return record


def _default_modalities(route: str) -> List[str]:
    mapping = {"image_text": ["image", "text"], "audio_text": ["audio", "text"],
               "video_text": ["video", "text"]}
    return list(mapping.get(route, ["text"]))


def _default_markers(route: str) -> List[str]:
    return ["<%s>" % route.split("_")[0]]


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


# --------------------------------------------------------------------------- #
# 写出工具
# --------------------------------------------------------------------------- #

def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """写出 JSONL；字符串元素按原文写出（用于构造坏行）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_json(path: Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# #23 交付包构造
# --------------------------------------------------------------------------- #

PACKAGE_MANIFEST_FORMAT = "crossmodal-delivery-package-v1"
PACKAGE_VERSION_FORMAT = "crossmodal-delivery-version-v1"
DEFAULT_ENTRY = "train/samples.jsonl"


def version_json(routes: Sequence[str] = ("image_text",),
                 *, status: str = "可交付", version_state: str = "deliverable",
                 dataset_version: str = "v1.0.0",
                 granularity: str = "whole") -> Dict[str, Any]:
    return {"format": PACKAGE_VERSION_FORMAT, "operator": "delivery_package",
            "operator_number": 23, "operator_version": "v1.0.0",
            "dataset_version": dataset_version, "enabled_routes": list(routes),
            "annotation_granularity": granularity, "status": status,
            "version_state": version_state,
            "packaged_records": 0, "isolation_excluded_from_package": True}


def package_manifest(*, entrypoint: str = DEFAULT_ENTRY,
                     version_state: str = "deliverable",
                     status: str = "可交付",
                     files: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    members = list(files or ())
    return {"format": PACKAGE_MANIFEST_FORMAT, "operator": "delivery_package",
            "operator_number": 23, "operator_version": "v1.0.0",
            "entrypoint": entrypoint, "version_state": version_state,
            "status": status, "deliverable": version_state == "deliverable",
            "missing_requirements": [] if version_state == "deliverable" else ["sampling_written_back"],
            "materials": {"quality_report": {"status": "found", "written_back": True},
                          "sampling_conclusion": {"status": "found", "written_back": True},
                          "isolation_list": {"status": "found"},
                          "version_notes": {"status": "found"}},
            "files": [{"path": name, "role": "other"} for name in members],
            "notes": "demo 交付包（测试夹具）"}


def quality_report(*, written_back: bool = True) -> Dict[str, Any]:
    return {"format": "crossmodal-quality-report-v1", "operator": "alignment_qc",
            "report_id": "qr-0001", "checks": {"openable": True},
            "status": "written_back" if written_back else "waiting_writeback",
            "written_back": bool(written_back),
            "writeback": {"completed": bool(written_back), "operator": "#24"}}


def sampling_conclusion(*, written_back: bool = True) -> Dict[str, Any]:
    return {"format": "crossmodal-sampling-conclusion-v1", "operator": "#24",
            "sampling_rate": 0.1, "status": "written_back" if written_back else "waiting_writeback",
            "writeback": {"completed": bool(written_back)}}


def version_notes(routes: Sequence[str] = ("image_text",)) -> Dict[str, Any]:
    return {"format": "crossmodal-version-notes-v1", "dataset_version": "v1.0.0",
            "enabled_routes": list(routes), "annotation_granularity": "whole"}


def isolation_list(sample_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
    return [{"sample_id": item, "label": "样本-语义不通",
             "destination": "sample_isolation_library"} for item in sample_ids]


def build_package(root: Path, *, records: Sequence[Any],
                  routes: Sequence[str] = ("image_text",),
                  version_state: str = "deliverable", status: str = "可交付",
                  entrypoint: str = DEFAULT_ENTRY,
                  isolation_ids: Sequence[str] = (),
                  materials: bool = True,
                  write_manifest: bool = True,
                  write_version: bool = True) -> Path:
    """构造一个 #23 交付包目录，返回入口 JSONL 的绝对路径。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    entry = write_jsonl(base / entrypoint, records)
    if materials:
        write_json(base / "materials" / "quality_report.json", quality_report())
        write_json(base / "materials" / "sampling_conclusion.json", sampling_conclusion())
        write_jsonl(base / "materials" / "isolation_list.jsonl",
                    isolation_list(isolation_ids))
        write_json(base / "materials" / "version_notes.json", version_notes(routes))
    if write_version:
        write_json(base / "version.json",
                   version_json(routes, status=status, version_state=version_state))
    if write_manifest:
        members = [entrypoint, "version.json", "manifest.json",
                   "materials/quality_report.json", "materials/sampling_conclusion.json",
                   "materials/isolation_list.jsonl", "materials/version_notes.json"]
        write_json(base / "manifest.json",
                   package_manifest(entrypoint=entrypoint, version_state=version_state,
                                    status=status, files=members))
    return entry


def package_root_of(entry: Path) -> Path:
    """从入口文件向上找到携带 manifest.json 的包根目录。"""
    for candidate in [Path(entry).parent, *Path(entry).parents]:
        if (candidate / "manifest.json").is_file():
            return candidate
    return Path(entry).parent


def build_zip(source_dir: Path, target: Path, *, prefix: str = "") -> Path:
    """把一个目录打成上传用 zip（成员路径为包内相对路径）。"""
    base = Path(source_dir)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                bundle.write(path, prefix + path.relative_to(base).as_posix())
    return target


def build_package_zip(tmp_path: Path, name: str = "package.zip", **kwargs: Any) -> Path:
    """构造「#23 交付包 zip」（上传场景）：包根目录即 zip 根。"""
    root = Path(tmp_path) / ("zip_src_" + name.replace(".zip", ""))
    build_package(root, **kwargs)
    return build_zip(root, Path(tmp_path) / name)


# --------------------------------------------------------------------------- #
# 默认记录集（覆盖各业务分支）
# --------------------------------------------------------------------------- #

def default_records(*, deliverable: bool = True) -> List[Any]:
    """覆盖 3 条路线 + 隔离 + 坏行 + 缺标识 + 非法媒体路径 + 重复标识。"""
    rows: List[Any] = []
    for index in range(1, 13):                       # 12 条图文（超 10 条上限）
        rows.append(sample_record("cm-1%03d" % index, route="image_text",
                                  description="图文样例描述 %02d" % index,
                                  score=score_block("image_text", value=0.30 + index / 100.0),
                                  annotation=annotation_block(),
                                  deliverable=deliverable))
    for index in range(1, 5):                        # 4 条音文（不足 10 条）
        rows.append(sample_record("cm-2%03d" % index, route="audio_text",
                                  description="音文样例描述 %02d" % index,
                                  score=score_block("audio_text", value=0.28,
                                                    operator_number=13,
                                                    model="wav2vec2-base"),
                                  annotation=annotation_block(),
                                  deliverable=deliverable))
    for index in range(1, 3):                        # 2 条视文（不足 10 条）
        rows.append(sample_record("cm-3%03d" % index, route="video_text",
                                  description="视文样例描述 %02d" % index,
                                  score=score_block("video_text", value=0.36,
                                                    operator_number=14),
                                  annotation=annotation_block(),
                                  deliverable=deliverable))
    rows.append(sample_record("cm-9911", route="image_text", description="隔离样本",
                              score=score_block("image_text", value=0.10),
                              isolation=isolation_marker("cm-9911"),
                              deliverable=deliverable))
    rows.append(sample_record("cm-9912", route="image_text", description="隔离清单样本",
                              score=score_block("image_text", value=0.11),
                              deliverable=deliverable))
    rows.append({"group_id": "grp-none", "modalities": ["image", "text"],
                 "routes": ["image_text"], "text": {"content": "缺标识"},
                 "operator_23": {"decision": "deliverable"}})
    rows.append(sample_record("cm-9913", route="image_text", description="绝对路径引用",
                              media=["C:/temp/escape.png"], deliverable=deliverable))
    rows.append(sample_record("cm-1001", route="image_text", description="重复标识",
                              deliverable=deliverable))
    rows.append('{"sample_id": "cm-bad-1", ')
    rows.append('["not", "an", "object"]')
    rows.append("")
    return rows


# --------------------------------------------------------------------------- #
# 配置夹具
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_YAML = """\
operator:
  number: 26
  name: 生成交付样例
  version: v1.0.0
  enabled: true
stage_id: S8
enabled: true
format: xlsx
source_status: 可交付
max_samples_per_route: 10
sampling_mode: random
sampling_seed: 26
description_summary_chars: 120
require_deliverable_source: false
require_deliverable_record: true
routes:
  image_text: true
  audio_text: false
  video_text: false
transport:
  format: crossmodal-files-v1
  max_response_bytes: 67108864
"""


@pytest.fixture()
def config_file(tmp_path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


def config_variant(base_config: Path, extra_yaml: str,
                   name: str = "config_variant.yaml") -> Path:
    """基于默认配置再叠加一段 YAML 覆盖，用于配置边界测试。"""
    path = Path(base_config).with_name(name)
    path.write_text(DEFAULT_CONFIG_YAML + extra_yaml, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def package_dir(tmp_path) -> Path:
    """默认「#23 可交付包」目录（三路线开启，12+4+2 条可抽样本）。"""
    return build_package(tmp_path / "task_pkg", records=default_records(),
                         routes=("image_text", "audio_text", "video_text"))


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
