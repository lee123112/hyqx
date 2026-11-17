"""算子18：元数据构建与数据集打包 —— 入库门禁、元数据构建、JSONL/TSV/ZIP 打包与 CLI。

处理边界：
- 本节点是「数据集交付」阶段的规则型算子，不执行任何模型推理，也不重新计算
  质量分、语义分或对齐置信度；上游分数原样透传。
- 只做四件事：入库门禁、元数据完整性检查与构建、JSONL/TSV/ZIP 原子打包、清单/日志。
- 不合格样本（C 级、领域范围外、低流畅、语义不等价、状态缺失、元数据缺失或冲突、
  重复 id）一律进入 ``18_rejects.jsonl``，绝不进入交付数据集。
- 未连接 MongoDB/Elasticsearch/Parquet，只在 manifest 预留索引描述。
- 无血缘改造（2026-10-19）：不再读取/生成/打包任何 lineage.json(l)；交付包只含业务产物。
  ``operator_versions`` 仅来自显式配置（无配置时为 ``{}`` 且 ``operator_versions_source=unavailable``），
  ``lineage_refs`` 恒为 ``[]`` 且 ``lineage_refs_source=unavailable``；阶段末返回前生成
  ``artifacts/stage_18_<uniqueId>.zip`` 并返回 ``stageZip``/``storageUri``。
"""
import argparse
import codecs
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, count_lines, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.packager import (TsvWriter, file_meta, remove_quietly, sha256_file,
                            write_json_atomic, write_zip_atomic)
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯；不再读取/生成 lineage 文件。
# from utils.lineage import (append_lineage, make_lineage, now_iso,
#                            write_lineage_jsonl_atomic)

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "metadata_dataset_packaging"
OPERATOR_VERSION = "v1.0.0"
OPERATOR_ID = "18"
DEFAULT_NAMES = {
    "dataset_jsonl_name": "18_dataset.jsonl",
    "dataset_tsv_name": "18_dataset.tsv",
    "manifest_name": "18_manifest.json",
    "package_name": "18_delivery.zip",
    "reject_name": "18_rejects.jsonl",
}

# [无血缘改造 2026-10-19] 原血缘阶段映射已停用：LINEAGE_OPERATOR_STAGES = {
#     "source_rights_admission": 1,
#     "structure_validation": 2,
#     "encoding_normalization": 3,
#     "blank_html_cleaning": 4,
#     "header_footer_timestamp_cleaning": 5,
#     "length_filter": 6,
#     "hash_deduplication": 7,
#     "sensitive_information_desensitization": 8,
#     "document_paragraph_alignment": 9,
#     "sentence_alignment": 10,
#     "clause_phrase_alignment": 11,
#     "alignment_confidence": 12,
#     "alignment_quality_grading": 13,
#     "semantic_equivalence": 14,
#     "translation_fluency": 15,
#     "domain_recognition": 16,
#     "entity_consistency": 17,
#     OPERATOR_NAME: 18,
# }


# --------------------------------------------------------------------------- #
# 配置与编码
# --------------------------------------------------------------------------- #

def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """按指定编码增量严格解码，逐行 yield 逻辑行（不整文件读入内存）。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    buffer = ""
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk, final=False)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def select_encoding(path, encodings: List[str]) -> str:
    """选择第一个能严格解码整个文件的编码，保证流式读取可用。"""
    if not encodings:
        raise ValueError("未配置输入编码")
    errors = []
    for encoding in encodings:
        try:
            for _ in iter_decoded_lines(path, encoding):
                pass
            return encoding.lower()
        except (LookupError, OSError, UnicodeDecodeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError("输入文件无法按配置编码解码：" + ",".join(errors))


# --------------------------------------------------------------------------- #
# 字段读取与元数据检查
# --------------------------------------------------------------------------- #

def _lookup(record: dict, metadata: dict, fields: List[str]):
    """按字段名列表取值：顶层优先，metadata 兜底。返回 (值, 命中字段, 来源)。"""
    for field in fields:
        value = record.get(field)
        if value is not None:
            return value, field, "top_level"
    for field in fields:
        value = metadata.get(field)
        if value is not None:
            return value, field, "metadata"
    return None, None, None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _normalize_for_compare(value: Any) -> Tuple[str, Any]:
    """把字段值规范成可比较的形式（数值与布尔分开，容器按稳定 JSON 比较）。"""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("text", value)
    return ("json", json.dumps(value, ensure_ascii=False, sort_keys=True))


def _detect_conflicts(record: dict, metadata: dict, fields: List[str]) -> Dict[str, dict]:
    """检测顶层字段与 metadata 同名键的真实差异，绝不静默覆盖。"""
    conflicts: Dict[str, dict] = {}
    for field in fields:
        top = record.get(field)
        mirrored = metadata.get(field)
        if top is None or mirrored is None:
            continue
        if _normalize_for_compare(top) != _normalize_for_compare(mirrored):
            conflicts[field] = {"top_level": top, "metadata": mirrored}
    return conflicts


def _resolve_conflicts(effective: dict, conflicts: Dict[str, dict],
                       policy: str) -> None:
    """按显式策略消解冲突（策略本身写入记录，属显式选择而非静默覆盖）。"""
    for field, values in conflicts.items():
        if policy == "prefer_metadata":
            effective[field] = values["metadata"]
        elif policy == "prefer_top_level":
            effective[field] = values["top_level"]


def _missing_fields(record: dict, metadata: dict, cfg: OperatorConfig,
                    operator_versions: dict) -> List[str]:
    missing: List[str] = []
    for field in cfg.required_fields:
        value = record.get(field)
        if value is None:
            value = metadata.get(field)
        if _is_blank(value):
            missing.append(field)
    if not cfg.derive_language_pair:
        value = _lookup(record, metadata, cfg.language_pair_fields)[0]
        if _is_blank(value):
            missing.append("language_pair")
    if cfg.require_operator_versions and not operator_versions:
        missing.append("operator_versions")
    # [无血缘改造 2026-10-19] lineage_refs 恒为空，不再作为必填校验项。
    return missing


def _number_reasons(record: dict, metadata: dict, cfg: OperatorConfig):
    """数值类型与量纲检查：不补分、不 clamp、不猜类型。"""
    reasons: List[str] = []
    details: Dict[str, dict] = {}
    checks = (
        (cfg.quality_score_fields, cfg.quality_score_range),
        (cfg.alignment_confidence_fields, cfg.alignment_confidence_range),
    )
    for fields, limits in checks:
        value, field, _ = _lookup(record, metadata, fields)
        if value is None:
            continue  # 字段缺失由 required_fields 统一处理
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append("invalid_type:%s" % field)
            details[field] = {"value_type": type(value).__name__}
            continue
        if math.isnan(value) or math.isinf(value):
            reasons.append("score_not_finite:%s" % field)
            details[field] = {"value": str(value)}
            continue
        if not (float(limits[0]) <= float(value) <= float(limits[1])):
            reasons.append("score_out_of_range:%s" % field)
            details[field] = {"value": value, "range": [limits[0], limits[1]]}
    return reasons, details


def _waiting_state(record: dict, metadata: dict, cfg: OperatorConfig):
    """判断记录是否处于 waiting_manual 状态；返回 (是否, 命中字段, 原值)。"""
    values = set(cfg.waiting_manual_values)
    for field in cfg.review_status_fields:
        value, source, _ = _lookup(record, metadata, [field])
        if isinstance(value, str) and value in values:
            return True, source, value
    for name, fields in cfg.status_fields.items():
        value, source, _ = _lookup(record, metadata, fields)
        if isinstance(value, str) and value in values:
            return True, source, value
    return False, None, None


def _grade_allowed(grade: Any, cfg: OperatorConfig) -> bool:
    if grade is None:
        return False
    value = str(grade)
    if value in cfg.allowed_grades:
        return True
    return bool(cfg.allow_c and value == "C")


def _fluency_gate_decision(record: dict, metadata: dict, cfg: OperatorConfig,
                           value: Any) -> Tuple[bool, Optional[str]]:
    """判断等待校准的流畅度状态是否只作辅助证据。

    仅当配置为 ``evidence_only_until_gold``、状态命中显式证据状态、且业务批准字段
    明确为 ``False`` 时才放行。批准字段缺失或为 True 时仍走严格门禁，避免用缺失值
    绕过业务批准。
    """
    if cfg.fluency_gate_mode != "evidence_only_until_gold":
        return False, None
    if str(value) not in cfg.fluency_evidence_statuses:
        return False, None
    approved, _, _ = _lookup(record, metadata, cfg.fluency_approval_fields)
    if approved is not False:
        return False, None
    return True, "waiting_calibration_until_gold_calibrated"


# 碎片形态判据（2026-11-04）：短记录里「真术语对」与「半截枚举/错位片段」靠长度分不开，
# 靠形态能分开。四个信号都取自 flow_15 实测的 37 条短记录：
#   1) 源文以顿号/逗号/分号/左括号收尾（`教育、`、`例如，"`）——枚举或引语被切断；
#   2) 译文以小写字母开头（`and capacity-building policies`）——上一句的续写；
#   3) 译文以逗号/分号收尾（`North Africa,`）——句子没说完；
#   4) 译文是裸日期/编号（`6-24 April 2015`）——与源文（`纽约`）跨位错配。
# 冒号不算碎片信号：`报告员：/Rapporteur:` 是完整的标题式术语对。
_FRAGMENT_TAIL_CHARS = "、，,；;（(［[「『“\"'‘《"
_FRAGMENT_HEAD_CHARS = "、，,；;）)」』”\"'’》"
_EN_MONTH_TOKENS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
_DATE_JOINER_CHARS = "-–—/.,:：年月日号"
_DATE_CJK_CHARS = "年月日号时分"


def _is_date_like(text: str) -> bool:
    """整段只由数字、日期连接符和月份名组成（`6-24 April 2015`、`2015年5月`）。"""
    tokens = re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]+", str(text or ""))
    if not tokens:
        return False
    for token in tokens:
        if token.isdigit():
            continue
        if token.isalpha():
            if token.lower() in _EN_MONTH_TOKENS:
                continue
            if all(ch in _DATE_CJK_CHARS for ch in token):
                continue
            # 其余字母是实词，说明这不是裸日期
            return False
        if all(ch in _DATE_JOINER_CHARS for ch in token):
            continue
        return False
    return True


def _is_fragment_shape(source: Any, target: Any) -> bool:
    """短记录是否呈碎片/错位形态；术语对（`增编/Addendum`）返回 False。"""
    source_text = str(source if source is not None else "").strip()
    target_text = str(target if target is not None else "").strip()
    if source_text and source_text[-1] in _FRAGMENT_TAIL_CHARS:
        return True
    if target_text and target_text[0] in _FRAGMENT_HEAD_CHARS:
        return True
    if target_text and target_text[0].islower():
        return True
    if target_text and target_text[-1] in "、，,；;":
        return True
    return _is_date_like(target_text)


def _gate(record: dict, metadata: dict, cfg: OperatorConfig, waiting: tuple):
    """入库门禁：返回 (拒绝原因列表, 状态值快照, 等级)。

    明确许可的 waiting_manual 记录（``allow_waiting_manual=true``）跳过业务门禁，
    但仍受上游「元数据完整性」门禁约束（在门禁之前已完成检查）。
    """
    grade, _, _ = _lookup(record, metadata, cfg.grade_fields)
    status_values: Dict[str, Any] = {}
    # 最小长度门禁（2026-11-04）：最终兜底，必须在 waiting_manual 旁路之前执行。
    # 上游碎片门禁漏掉的超短片段不得进入交付包。
    # short_record_policy=fragment_only（默认）时，短记录还须命中碎片形态才拒收，
    # 否则按术语对放行并在交付记录里留 short_record_exempt 标记，供业务抽样复核。
    length_reasons: List[str] = []
    for field, minimum in (("source", cfg.min_source_chars), ("target", cfg.min_target_chars)):
        if minimum <= 0:
            continue
        value, _, _ = _lookup(record, metadata, [field])
        if isinstance(value, str) and len("".join(value.split())) < minimum:
            length_reasons.append("too_short_%s" % field)
    if length_reasons:
        fragment = _is_fragment_shape(_lookup(record, metadata, ["source"])[0],
                                      _lookup(record, metadata, ["target"])[0])
        if cfg.short_record_policy == "always" or fragment:
            return length_reasons, status_values, grade
        status_values["short_record_exempt"] = "term_pair"
    # 这是硬安全门禁，必须在 waiting_manual 旁路之前执行。
    # waiting_manual 只表示允许暂存待复核，不表示实体冲突可进入临时交付包。
    if cfg.reject_inconsistent_entities:
        entity_status, _, _ = _lookup(record, metadata, cfg.entity_status_fields)
        if entity_status is not None:
            status_values["entity"] = entity_status
            if str(entity_status) not in cfg.accepted_entity_statuses:
                return ["entity_consistency_not_accepted"], status_values, grade
    if waiting[0] and cfg.allow_waiting_manual:
        for name, fields in cfg.status_fields.items():
            value, _, _ = _lookup(record, metadata, fields)
            if value is not None:
                status_values[name] = value
        return [], status_values, grade

    reasons: List[str] = []
    if not _grade_allowed(grade, cfg):
        reasons.append("grade_not_allowed")
    if cfg.respect_delivery_eligible:
        eligible, _, _ = _lookup(record, metadata, cfg.delivery_eligible_fields)
        if eligible is False:
            reasons.append("not_delivery_eligible")
    for name, fields in cfg.status_fields.items():
        value, _, _ = _lookup(record, metadata, fields)
        if value is None:
            if cfg.require_status_fields:
                reasons.append("missing_status:%s" % name)
            continue
        status_values[name] = value
        if name == "fluency":
            evidence_only, gate_status = _fluency_gate_decision(
                record, metadata, cfg, value)
            if evidence_only:
                status_values["fluency_gate"] = gate_status
                status_values["fluency_evidence_only"] = True
                continue
        if str(value) not in cfg.accepted_status_map.get(name, []):
            reasons.append("%s_status_not_accepted" % name)
    if waiting[0]:
        reasons.append("waiting_manual")
    return reasons, status_values, grade


# --------------------------------------------------------------------------- #
# 上游元数据上下文（无血缘改造 2026-10-19）
# --------------------------------------------------------------------------- #
# [无血缘改造 2026-10-19] 原「读取上游阶段血缘 / 拼接血缘记录 / 统计血缘阶段号 /
# 校验交付包血缘归档」实现已全部停用：不再读取任何 lineage.json / lineage.jsonl，
# 交付包也不含血缘成员。原实现整段保留于注释，便于回溯。


def _build_operator_context(cfg: OperatorConfig, input_file: str, tdir: Path,
                            unique_id: str) -> dict:
    """无血缘改造：operator_versions 仅取显式配置；lineage_refs 恒为空。"""
    versions: Dict[str, str] = dict(cfg.operator_versions or {})
    return {
        "upstream_records": [],
        "upstream_sources": [],
        "upstream_path": None,
        "operator_versions": versions,
        "operator_versions_source": "config" if versions else "unavailable",
        "lineage_refs": [],
        "lineage_refs_source": "unavailable",
        "model_versions": {},
    }


# ---- 以下为原血缘故实现（已停用，仅保留便于回溯） ----
# # --------------------------------------------------------------------------- #
# # 上游血缘与元数据构建
# # --------------------------------------------------------------------------- #

# def _parse_stage(value):
#     if isinstance(value, bool):
#         return None
#     if isinstance(value, int):
#         return value
#     if isinstance(value, str) and value.strip().isdigit():
#         return int(value.strip())
#     return None


# def _record_operator(record: dict):
#     if record.get("type") == "stage_segment":
#         operators = record.get("operators") or []
#         if operators and isinstance(operators[0], dict):
#             tool = operators[0].get("tool") or {}
#             if isinstance(tool, dict):
#                 return tool.get("id") or tool.get("name")
#     return record.get("operator")


# def _stage_from_path(path) -> Optional[int]:
#     for part in reversed(Path(path).parts):
#         match = re.search(r"(?:^|[_-])run(\d{1,2})(?:[_-]|$)", part, re.IGNORECASE)
#         if match:
#             return int(match.group(1))
#         match = re.search(r"(?:^|[_-])operator[_-]?(\d{1,2})(?:[_-]|$)", part, re.IGNORECASE)
#         if match:
#             return int(match.group(1))
#     return None


# def _stage_from_record(record: dict, source_path=None) -> Optional[int]:
#     stage = _parse_stage(record.get("stage"))
#     if stage is not None:
#         return stage
#     if record.get("type") == "stage_segment":
#         operators = record.get("operators") or []
#         if operators and isinstance(operators[0], dict):
#             stage = _parse_stage(operators[0].get("seq"))
#             if stage is not None:
#                 return stage
#     operator = _record_operator(record)
#     stage = LINEAGE_OPERATOR_STAGES.get(operator)
#     return stage if stage is not None else _stage_from_path(source_path) if source_path else None


# def _read_lineage_records(path) -> List[dict]:
#     text = Path(path).read_text(encoding="utf-8-sig")
#     try:
#         parsed = json.loads(text)
#     except json.JSONDecodeError:
#         parsed = None
#     if isinstance(parsed, list):
#         return [item for item in parsed if isinstance(item, dict)]
#     if isinstance(parsed, dict):
#         return [parsed]
#     records = []
#     for line in text.splitlines():
#         if line.strip():
#             value = json.loads(line)
#             if isinstance(value, dict):
#                 records.append(value)
#     return records


# def _task_family(unique_id: str) -> Optional[str]:
#     match = re.match(r"^(.*?)(?:_)?run\d{1,2}(?:_|$)", str(unique_id), re.IGNORECASE)
#     if not match:
#         return None
#     return match.group(1).rstrip("_-") or None


# def _path_family(path) -> Optional[str]:
#     for part in reversed(Path(path).parts):
#         match = re.match(r"^(.*?)(?:_)?run\d{1,2}(?:_|$)", part, re.IGNORECASE)
#         if match:
#             return match.group(1).rstrip("_-") or None
#     return None


# def _lineage_identity(record: dict):
#     operator = _record_operator(record)
#     if record.get("type") == "stage_segment":
#         operators = record.get("operators") or []
#         operator_entry = operators[0] if operators and isinstance(operators[0], dict) else {}
#         request = operator_entry.get("request") or {}
#         return json.dumps({
#             "type": record.get("type"),
#             "stage": _stage_from_record(record),
#             "operator": operator,
#             "uniqueId": request.get("uniqueId"),
#             "input": request.get("input"),
#             "output": operator_entry.get("output"),
#             "action": (operator_entry.get("process") or {}).get("action"),
#         }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
#     return json.dumps({
#         "type": record.get("type", "legacy"),
#         "stage": _stage_from_record(record),
#         "operator": operator,
#         "uniqueId": record.get("uniqueId"),
#         "input": record.get("inputFile"),
#         "output": record.get("outputFile"),
#         "status": record.get("status"),
#     }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# def _lineage_candidates(cfg: OperatorConfig, input_file: str, tdir: Path):
#     name = cfg.upstream_lineage_file
#     candidates = []
#     if name:
#         raw = Path(name)
#         names = [raw]
#         if raw.name == "lineage.jsonl":
#             names.append(raw.with_name("lineage.json"))
#         elif raw.name == "lineage.json":
#             names.append(raw.with_name("lineage.jsonl"))
#         if raw.is_absolute():
#             candidates.extend(names)
#         else:
#             for root in (Path(input_file).resolve().parent, Path(tdir), BASE_DIR):
#                 candidates.extend(root / item for item in names)
#     return [item.resolve() for item in candidates]


# def _collect_task_lineage(cfg: OperatorConfig, input_file: str, tdir: Path,
#                           unique_id: str):
#     explicit = {path for path in _lineage_candidates(cfg, input_file, tdir) if path.is_file()}
#     roots = [Path(input_file).resolve().parent]
#     input_parent = Path(input_file).resolve().parent
#     if _stage_from_path(input_parent) is not None:
#         roots.append(input_parent.parent)
#     if tdir.parent.is_dir():
#         roots.append(tdir.parent)

#     paths = []
#     for path in explicit:
#         paths.append((path, True))
#     seen_paths = {path for path, _ in paths}
#     for root in roots:
#         if not root.is_dir():
#             continue
#         for name in ("lineage.json", "lineage.jsonl"):
#             for path in root.rglob(name):
#                 path = path.resolve()
#                 if path in seen_paths or "__pycache__" in path.parts:
#                     continue
#                 seen_paths.add(path)
#                 paths.append((path, False))

#     family = _task_family(unique_id)
#     entries = []
#     for path, is_explicit in paths:
#         try:
#             records = _read_lineage_records(path)
#         except (OSError, UnicodeError, json.JSONDecodeError):
#             continue
#         source_family = _path_family(path)
#         for index, record in enumerate(records):
#             stage = _stage_from_record(record, path)
#             if stage is not None and not 1 <= stage <= 17:
#                 continue
#             if not is_explicit and family and source_family and source_family != family:
#                 continue
#             if not is_explicit and family and source_family is None:
#                 record_uid = str(record.get("uniqueId") or "")
#                 if not record_uid.startswith(family + "_run"):
#                     continue
#             entries.append({
#                 "record": record,
#                 "path": path,
#                 "index": index,
#                 "stage": stage,
#             })

#     entries.sort(key=lambda item: (
#         item["stage"] if item["stage"] is not None else 10**6,
#         str(item["path"]).lower(),
#         item["index"],
#     ))
#     result = []
#     seen = set()
#     for entry in entries:
#         key = _lineage_identity(entry["record"])
#         if key in seen:
#             continue
#         seen.add(key)
#         result.append(entry)
#     return result


# def _build_operator_context(cfg: OperatorConfig, input_file: str, tdir: Path,
#                             unique_id: str) -> dict:
#     entries = _collect_task_lineage(cfg, input_file, tdir, unique_id)
#     upstream = [entry["record"] for entry in entries]
#     upstream_sources = [entry["path"] for entry in entries]
#     upstream = [entry for entry in upstream if _record_operator(entry) != OPERATOR_NAME]
#     upstream_sources = upstream_sources[:len(upstream)]
#     versions: Dict[str, str] = dict(cfg.operator_versions or {})
#     from_config = bool(versions)
#     for entry in upstream:
#         operator = _record_operator(entry)
#         version = entry.get("operator_version")
#         if entry.get("type") == "stage_segment":
#             operators = entry.get("operators") or []
#             tool = (operators[0].get("tool") if operators and isinstance(operators[0], dict) else {}) or {}
#             version = tool.get("version")
#         if isinstance(operator, str) and isinstance(version, str) and version.strip():
#             versions[operator] = version
#     if upstream:
#         versions_source = "upstream_lineage"
#     elif from_config:
#         versions_source = "config"
#     else:
#         versions_source = "unavailable"

#     refs: List[str] = []
#     if upstream:
#         for index, entry in enumerate(upstream):
#             label = _record_operator(entry)
#             source = upstream_sources[index] if index < len(upstream_sources) else None
#             lineage_name = Path(source).name if source else Path(cfg.upstream_lineage_file).name
#             refs.append("%s#%d:%s" % (lineage_name, index, label))
#     refs_source = "upstream_lineage" if refs else "unavailable"

#     model_versions: Dict[str, Any] = {}
#     for entry in upstream:
#         statistics = entry.get("statistics")
#         if entry.get("type") == "stage_segment":
#             ops = entry.get("operators") or []
#             proc = (ops[0].get("process") if ops and isinstance(ops[0], dict) else {}) or {}
#             statistics = proc.get("metrics") or {}
#         version = statistics.get("model_version") if isinstance(statistics, dict) else None
#         if isinstance(version, str) and version.strip():
#             model_versions[str(_record_operator(entry) or "unknown")] = version

#     return {
#         "upstream_records": upstream,
#         "upstream_sources": upstream_sources,
#         "upstream_path": upstream_sources[0] if upstream_sources else None,
#         "operator_versions": versions,
#         "operator_versions_source": versions_source,
#         "lineage_refs": refs,
#         "lineage_refs_source": refs_source,
#         "model_versions": model_versions,
#     }


# def _lineage_stage_numbers(records: List[dict]) -> List[int]:
#     return sorted({stage for stage in (_stage_from_record(record) for record in records)
#                    if stage is not None})


# def _validate_lineage_archive(package_file: Path, expected_stages: List[int]) -> List[int]:
#     with zipfile.ZipFile(package_file) as archive:
#         if "lineage.jsonl" not in archive.namelist():
#             raise ValueError("交付包缺少 lineage.jsonl")
#         records = []
#         for line in archive.read("lineage.jsonl").decode("utf-8-sig").splitlines():
#             if line.strip():
#                 value = json.loads(line)
#                 if isinstance(value, dict):
#                     records.append(value)
#     actual = _lineage_stage_numbers(records)
#     if not set(expected_stages).issubset(actual):
#         missing = sorted(set(expected_stages) - set(actual))
#         raise ValueError("交付包 lineage.jsonl 缺少阶段：%s" % ",".join("%02d" % item for item in missing))
#     if not actual or actual[-1] != 18:
#         raise ValueError("交付包 lineage.jsonl 最终阶段不是 18")
#     return actual



def _build_delivery_record(record: dict, metadata: dict, cfg: OperatorConfig, context: dict,
                           packaged_at: str, waiting: tuple, grade: Any,
                           conflicts: Dict[str, dict]) -> dict:
    """构建交付记录：上游字段原样保留，补齐并镜像交付元数据。"""
    language_pair, _, _ = _lookup(record, metadata, cfg.language_pair_fields)
    derived = False
    if _is_blank(language_pair):
        source_lang, _, _ = _lookup(record, metadata, ["source_lang"])
        target_lang, _, _ = _lookup(record, metadata, ["target_lang"])
        language_pair = "%s%s%s" % (source_lang, cfg.language_pair_separator, target_lang)
        derived = True

    quality_score = _lookup(record, metadata, cfg.quality_score_fields)[0]
    alignment_confidence = _lookup(record, metadata, cfg.alignment_confidence_fields)[0]
    domain = _lookup(record, metadata, ["domain"])[0]
    source_ref = _lookup(record, metadata, ["source_ref"])[0]

    output = dict(record)
    output["quality_grade"] = grade
    output["quality_score"] = float(quality_score)
    output["alignment_confidence"] = float(alignment_confidence)
    output["domain"] = domain
    output["source_ref"] = source_ref
    output["language_pair"] = language_pair
    output["language_pair_derived"] = derived
    output["operator_versions"] = dict(context["operator_versions"])
    output["operator_versions_source"] = context["operator_versions_source"]
    output["lineage_refs"] = list(context["lineage_refs"])
    output["lineage_refs_source"] = context["lineage_refs_source"]
    output["created_at"] = packaged_at
    output["package_version"] = cfg.package_version
    output["delivery_status"] = "waiting_manual" if waiting[0] else "delivered"
    output["delivery_checked_at"] = packaged_at

    fluency_value, _, _ = _lookup(record, metadata, cfg.status_fields.get("fluency", []))
    fluency_evidence_only, fluency_gate_status = _fluency_gate_decision(
        record, metadata, cfg, fluency_value)
    if fluency_evidence_only:
        output["fluency_gate"] = fluency_gate_status
        output["fluency_evidence_only"] = True

    meta_out = dict(metadata)
    meta_out.update({
        "quality_grade": grade,
        "quality_score": output["quality_score"],
        "alignment_confidence": output["alignment_confidence"],
        "domain": domain,
        "source_ref": source_ref,
        "language_pair": language_pair,
        "language_pair_derived": derived,
        "operator_versions": dict(context["operator_versions"]),
        "lineage_refs": list(context["lineage_refs"]),
        "created_at": packaged_at,
        "package_version": cfg.package_version,
        "delivery_status": output["delivery_status"],
    })
    if fluency_evidence_only:
        meta_out["fluency_gate"] = fluency_gate_status
        meta_out["fluency_evidence_only"] = True
    if conflicts:
        meta_out["metadata_conflict_fields"] = sorted(conflicts)
        meta_out["metadata_conflict_policy"] = cfg.metadata_conflict_policy
        output["metadata_conflict_fields"] = sorted(conflicts)
        output["metadata_conflict_policy"] = cfg.metadata_conflict_policy
    if waiting[0]:
        meta_out["waiting_manual_field"] = waiting[1]
        meta_out["waiting_manual_value"] = waiting[2]
    output["metadata"] = meta_out
    return output


# --------------------------------------------------------------------------- #
# 统计与清单
# --------------------------------------------------------------------------- #

def _base_statistics() -> Dict[str, Any]:
    return {
        "input_records": 0,
        "accepted_records": 0,
        "rejected_records": 0,
        "delivery_records": 0,
        "S_count": 0,
        "A_count": 0,
        "C_count": 0,
        "waiting_manual_count": 0,
        "delivered_waiting_manual_records": 0,
        "fluency_evidence_only_records": 0,
        "short_record_exempt_records": 0,
        "duplicate_id_records": 0,
        "metadata_conflict_records": 0,
        "missing_metadata_records": 0,
        "operator_versions_unavailable": 0,
        "bytes": 0,
        "sha256": None,
        "tsv_bytes": 0,
        "tsv_sha256": None,
        "manifest_bytes": 0,
        "manifest_sha256": None,
        "package_bytes": 0,
        "package_sha256": None,
        "package_version": None,
        "pipeline_version": None,
        "manifest_version": None,
        "language_pair": None,
        "unit_type": None,
        "unit_type_counts": {},
        "input_unit_type_counts": {},
        "input_language_pair_counts": {},
        "grade_counts": {},
        "delivery_grade_counts": {},
        "source_types": {},
        "model_versions": {},
        "rule_versions": {},
        "zip_members": [],
        "encoding": None,
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, "reason:%s" % reason)


def _reject_entry(lineno: int, record: Any, reasons: List[str], **extra) -> dict:
    entry: Dict[str, Any] = {
        "line": lineno,
        "id": None,
        "source_ref": None,
        "reasons": list(reasons),
        "record": record,
    }
    if isinstance(record, dict):
        entry["id"] = record.get("id")
        entry["source_ref"] = record.get("source_ref")
    for key, value in extra.items():
        if value:
            entry[key] = value
    return entry


def _most_common(counter: Counter, fallback):
    if not counter:
        return fallback
    return counter.most_common(1)[0][0]


def _scope_unit_type(record: dict, metadata: dict, cfg: OperatorConfig):
    """Resolve the real business unit without confusing granularity labels."""
    value, _, _ = _lookup(record, metadata, ["unit_type"])
    if not _is_blank(value):
        return str(value)
    value, _, _ = _lookup(record, metadata, cfg.unit_type_fields)
    if not _is_blank(value):
        return str(value)
    return cfg.default_unit_type


def _scope_language_pair(record: dict, metadata: dict, cfg: OperatorConfig):
    value, _, _ = _lookup(record, metadata, cfg.language_pair_fields)
    if not _is_blank(value):
        return str(value)
    source_lang, _, _ = _lookup(record, metadata, ["source_lang"])
    target_lang, _, _ = _lookup(record, metadata, ["target_lang"])
    if not _is_blank(source_lang) and not _is_blank(target_lang):
        return "%s%s%s" % (source_lang, cfg.language_pair_separator, target_lang)
    return None


def _collect_delivery_metadata(records: List[dict], cfg: OperatorConfig,
                               context: dict) -> Dict[str, Any]:
    """从真实交付记录统计 unit_type / language_pair / 来源 / 模型与规则版本。"""
    unit_types: Counter = Counter()
    language_pairs: Counter = Counter()
    source_types: Counter = Counter()
    model_versions: Dict[str, Any] = dict(context["model_versions"])
    rule_versions: Dict[str, List[str]] = {}

    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        unit_types[_scope_unit_type(record, metadata, cfg)] += 1
        language_pair = _scope_language_pair(record, metadata, cfg)
        language_pairs[str(language_pair)] += 1
        source, _, _ = _lookup(record, metadata, cfg.source_type_fields)
        source_types[str(source) if source is not None else "unknown"] += 1
        for field in cfg.model_version_fields:
            value = record.get(field)
            if value is None:
                value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                model_versions.setdefault(field, value)
        for field in cfg.rule_version_fields:
            value = record.get(field)
            if value is None:
                value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                rule_versions.setdefault(field, [])
                if value not in rule_versions[field]:
                    rule_versions[field].append(value)

    return {
        "unit_type": _most_common(unit_types, cfg.default_unit_type),
        "unit_type_counts": dict(unit_types),
        "language_pair": _most_common(language_pairs, None),
        "language_pair_counts": dict(language_pairs),
        "source_types": dict(source_types),
        "model_versions": model_versions,
        "rule_versions": {k: sorted(v) for k, v in rule_versions.items()},
    }


def _build_manifest(cfg: OperatorConfig, unique_id: str, packaged_at: str,
                    statistics: Dict[str, Any], collected: Dict[str, Any],
                    files: List[dict], delivered_records: List[dict]) -> dict:
    manifest_statistics = {key: value for key, value in statistics.items()
                           if key not in {"package_bytes", "package_sha256"}}
    return {
        "manifest_version": cfg.manifest_version,
        "package_version": cfg.package_version,
        "pipeline_version": cfg.pipeline_version,
        "task_id": unique_id,
        "created_at": packaged_at,
        "generated_by": {"operator": OPERATOR_NAME, "operator_version": OPERATOR_VERSION},
        "unit_type": collected["unit_type"] if statistics["delivery_records"] else None,
        "unit_type_counts": collected["unit_type_counts"],
        "language_pair": collected["language_pair"] if statistics["delivery_records"] else None,
        "language_pair_counts": collected["language_pair_counts"],
        "language_pair_mixed": len(collected["language_pair_counts"]) > 1,
        "input_record_count": statistics["input_records"],
        "input_unit_type_counts": statistics["input_unit_type_counts"],
        "input_language_pair_counts": statistics["input_language_pair_counts"],
        "input_unit_type": _most_common(
            Counter(statistics["input_unit_type_counts"]), None
        ),
        "input_language_pair": _most_common(
            Counter(statistics["input_language_pair_counts"]), None
        ),
        "input_unit_type_mixed": len(statistics["input_unit_type_counts"]) > 1,
        "input_language_pair_mixed": len(statistics["input_language_pair_counts"]) > 1,
        "delivery_record_count": statistics["delivery_records"],
        "source_types": collected["source_types"],
        "model_versions": collected["model_versions"],
        "rule_versions": collected["rule_versions"],
        "operator_versions_source": statistics.get("operator_versions_source"),
        # [无血缘改造 2026-10-19] 不再有血缘引用；来源固定标记 unavailable。
        "lineage_refs_source": statistics.get("lineage_refs_source", "unavailable"),
        "record_count": statistics["delivery_records"],
        "fluency_gate": {
            "mode": cfg.fluency_gate_mode,
            "approval_fields": list(cfg.fluency_approval_fields),
            "evidence_only_statuses": list(cfg.fluency_evidence_statuses),
            "evidence_only_records": statistics.get("fluency_evidence_only_records", 0),
            "blocking": cfg.fluency_gate_mode == "enforce",
            "task_book_policy": (
                "规范 4.15：Gold 校准完成前 waiting_calibration 仅作辅助证据，"
                "不作为放行/拦截的最终依据。"),
        },
        "statistics": manifest_statistics,
        "files": files,
        "package": {
            "name": cfg.package_name,
            "members": list(cfg.zip_members),
            "sha256": None,
            "note": "zip 自身 sha256 记录在 statistics.package_sha256；清单在 zip 内不能自引用",
        },
        "indexes": cfg.indexes,
        "metadata_conflict_policy": cfg.metadata_conflict_policy,
        "missing_metadata_policy": "reject",
        "sample_record_keys": sorted(delivered_records[0].keys()) if delivered_records else [],
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _failure(logger, unique_id, input_file, output_file, cfg_snapshot,
             statistics, message, started, reject_file=None):
    # [无血缘改造 2026-10-19] 失败分支不再写血缘、不再返回 lineageFile；阶段产物为 None。
    log_action(logger, "失败", message)
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": reject_file,
            "manifestFile": None, "packageFile": None,
            "stageZip": None, "storageUri": None,
            "statistics": statistics, "message": message}


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        output_file = tdir / DEFAULT_NAMES["dataset_jsonl_name"]
        logger = logger or get_logger("%s.%s" % (OPERATOR_NAME, unique_id), tdir / "run.log")
        message = "配置错误：%s" % type(exc).__name__
        return _failure(logger, unique_id, input_file, output_file,
                        {"config_error": message}, _base_statistics(), message, now_iso())

    jsonl_file = tdir / cfg.dataset_jsonl_name
    tsv_file = tdir / cfg.dataset_tsv_name
    manifest_file = tdir / cfg.manifest_name
    package_file = tdir / cfg.package_name
    reject_file = tdir / cfg.reject_name
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.jsonl"
    log_file = tdir / "run.log"
    tmp_jsonl = jsonl_file.with_name(jsonl_file.name + ".tmp")
    tmp_tsv = tsv_file.with_name(tsv_file.name + ".tmp")
    tmp_reject = reject_file.with_name(reject_file.name + ".tmp")

    logger = logger or get_logger("%s.%s" % (OPERATOR_NAME, unique_id), log_file)
    started = now_iso()
    statistics = _base_statistics()
    statistics["package_version"] = cfg.package_version
    statistics["pipeline_version"] = cfg.pipeline_version
    statistics["manifest_version"] = cfg.manifest_version
    statistics["zip_members"] = list(cfg.zip_members)

    protected = {p.resolve() for p in
                 (jsonl_file, tsv_file, manifest_file, package_file, reject_file, log_file)}
    if Path(input_file).resolve() in protected:
        return _failure(logger, unique_id, input_file, jsonl_file,
                        cfg_snapshot, statistics, "输入文件与输出文件路径相同，拒绝覆盖输入",
                        started)

    log_action(logger, "输入", "加载输入文件：%s" % Path(input_file).name)
    log_action(logger, "配置", "允许等级=%s C许可=%s 待人工许可=%s 冲突策略=%s 空交付策略=%s"
               % (cfg.allowed_grades, cfg.allow_c, cfg.allow_waiting_manual,
                  cfg.metadata_conflict_policy, cfg.empty_delivery_policy))

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        return _failure(logger, unique_id, input_file, jsonl_file,
                        cfg_snapshot, statistics,
                        "输入文件编码处理失败：%s，请检查文件编码" % type(exc).__name__, started)
    statistics["encoding"] = encoding
    log_action(logger, "读取", "解码输入文件：%s" % encoding)

    context = _build_operator_context(cfg, input_file, tdir, unique_id)
    statistics["operator_versions_source"] = context["operator_versions_source"]
    statistics["lineage_refs_source"] = "unavailable"
    if context["operator_versions"]:
        log_action(logger, "来源", "operator_versions 来自显式配置（%d 项）；"
                   "lineage_refs 标记为 unavailable" % len(context["operator_versions"]))
    else:
        log_action(logger, "来源", "未配置 operator_versions 且不读取上游血缘，"
                   "operator_versions 标记为 unavailable")

    packaged_at = now_iso()
    grade_counts: Counter = Counter()
    delivery_grade_counts: Counter = Counter()
    status_value_counts: Counter = Counter()
    seen_ids: Dict[str, int] = {}
    delivered_records: List[dict] = []
    internal_error: Optional[str] = None

    try:
        with JsonlWriter(tmp_jsonl) as dataset_writer, \
                JsonlWriter(tmp_reject) as reject_writer, \
                TsvWriter(tmp_tsv, cfg.tsv_fields, cfg.tsv_delimiter) as tsv_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"]))
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"]))
                    _append_reason(statistics, "invalid_record")
                    continue

                metadata = record.get("metadata")
                if metadata is None:
                    metadata = {}
                if not isinstance(metadata, dict):
                    reject_writer.write(_reject_entry(lineno, record, ["invalid_type:metadata"]))
                    _append_reason(statistics, "invalid_type:metadata")
                    continue

                # Record the input scope before quality gates reject a record.
                # Delivery metadata is intentionally tracked separately: a
                # zero-record formal package must still explain its input
                # business unit and language pair.
                input_unit = _scope_unit_type(record, metadata, cfg)
                statistics["input_unit_type_counts"][input_unit] = (
                    statistics["input_unit_type_counts"].get(input_unit, 0) + 1
                )
                input_language_pair = _scope_language_pair(record, metadata, cfg)
                if input_language_pair is not None:
                    statistics["input_language_pair_counts"][input_language_pair] = (
                        statistics["input_language_pair_counts"].get(input_language_pair, 0) + 1
                    )

                grade, _, _ = _lookup(record, metadata, cfg.grade_fields)
                if not _is_blank(grade):
                    grade_counts[str(grade)] += 1
                waiting = _waiting_state(record, metadata, cfg)
                if waiting[0]:
                    _bump(statistics, "waiting_manual_count")

                # 1) metadata 冲突：默认拒收，绝不静默覆盖。
                conflicts = _detect_conflicts(record, metadata, cfg.conflict_check_fields)
                effective = dict(record)
                if conflicts:
                    if cfg.metadata_conflict_policy == "reject":
                        reject_writer.write(_reject_entry(
                            lineno, record, ["metadata_conflict"], conflicts=conflicts))
                        _append_reason(statistics, "metadata_conflict")
                        _bump(statistics, "metadata_conflict_records")
                        continue
                    _resolve_conflicts(effective, conflicts, cfg.metadata_conflict_policy)

                # 2) 必填元数据完整性：缺失即 missing_metadata，不用空值补齐。
                missing = _missing_fields(effective, metadata, cfg,
                                          context["operator_versions"])
                if missing:
                    # Keep metadata completeness as a hard rejection, but also
                    # evaluate the independent business gates so the reject
                    # record explains every applicable reason (domain/model
                    # unavailable/entity conflict), rather than masking them
                    # behind only ``missing_metadata``.
                    gate_reasons, status_values, _ = _gate(effective, metadata, cfg, waiting)
                    combined_reasons = ["missing_metadata"] + [
                        reason for reason in gate_reasons if reason != "missing_metadata"]
                    reject_writer.write(_reject_entry(
                        lineno, record, combined_reasons, missing_fields=missing,
                        status_values=status_values))
                    _append_reason(statistics, "missing_metadata")
                    _bump(statistics, "missing_metadata_records")
                    for reason in gate_reasons:
                        _append_reason(statistics, reason)
                    continue

                # 3) 数值类型与量纲。
                number_reasons, number_details = _number_reasons(effective, metadata, cfg)
                if number_reasons:
                    reject_writer.write(_reject_entry(
                        lineno, record, number_reasons, details=number_details))
                    for reason in number_reasons:
                        _append_reason(statistics, reason)
                    continue

                # 4) 入库门禁。
                reasons, status_values, grade = _gate(effective, metadata, cfg, waiting)
                for name, value in status_values.items():
                    status_value_counts["%s=%s" % (name, value)] += 1
                if reasons:
                    reject_writer.write(_reject_entry(
                        lineno, record, reasons, status_values=status_values))
                    for reason in reasons:
                        _append_reason(statistics, reason)
                    continue

                # 5) 重复 id：默认拒收，避免交付集内出现同 id 冲突。
                record_id = record.get("id")
                is_duplicate = record_id in seen_ids
                if is_duplicate:
                    _bump(statistics, "duplicate_id_records")
                    if cfg.duplicate_id_policy == "reject":
                        reject_writer.write(_reject_entry(
                            lineno, record, ["duplicate_id"], first_seen_line=seen_ids[record_id]))
                        _append_reason(statistics, "duplicate_id")
                        continue
                else:
                    seen_ids[record_id] = lineno

                # 6) 构建并写出交付记录。
                # 短但呈术语形态的记录：把豁免标记写进 metadata，避免"悄悄放行"。
                if status_values.get("short_record_exempt"):
                    metadata = dict(metadata)
                    metadata["short_record_exempt"] = status_values["short_record_exempt"]
                    _bump(statistics, "short_record_exempt_records")
                delivery = _build_delivery_record(
                    effective, metadata, cfg, context, packaged_at, waiting, grade, conflicts)
                if is_duplicate:
                    delivery["metadata"]["duplicate_id"] = True
                dataset_writer.write(delivery)
                tsv_writer.write(delivery)
                delivered_records.append(delivery)
                statistics["accepted_records"] += 1
                delivery_grade_counts[str(grade)] += 1
                if delivery.get("fluency_evidence_only"):
                    _bump(statistics, "fluency_evidence_only_records")
                if waiting[0]:
                    _bump(statistics, "delivered_waiting_manual_records")
    except (OSError, UnicodeDecodeError) as exc:
        remove_quietly(tmp_jsonl, tmp_tsv, tmp_reject)
        return _failure(logger, unique_id, input_file, jsonl_file,
                        cfg_snapshot, statistics,
                        "输入文件读取失败：%s" % type(exc).__name__, started)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免留下半成品与未关闭句柄
        remove_quietly(tmp_jsonl, tmp_tsv, tmp_reject)
        return _failure(logger, unique_id, input_file, jsonl_file,
                        cfg_snapshot, statistics,
                        "处理失败：%s" % type(exc).__name__, started)

    if statistics["input_records"] == 0:
        remove_quietly(tmp_jsonl, tmp_tsv, tmp_reject)
        return _failure(logger, unique_id, input_file, jsonl_file,
                        cfg_snapshot, statistics, "输入文件为空或无有效记录", started)

    # 原子替换：先关闭句柄再替换，避免 Windows 文件占用与下游读到半成品。
    os.replace(tmp_jsonl, jsonl_file)
    os.replace(tmp_tsv, tsv_file)
    os.replace(tmp_reject, reject_file)

    statistics["delivery_records"] = count_lines(jsonl_file)
    statistics["rejected_records"] = count_lines(reject_file)
    if statistics["delivery_records"] != statistics["accepted_records"]:
        internal_error = "交付记录数与写入数不一致"
        log_action(logger, "警告", internal_error)

    statistics["S_count"] = grade_counts.get("S", 0)
    statistics["A_count"] = grade_counts.get("A", 0)
    statistics["C_count"] = grade_counts.get("C", 0)
    statistics["grade_counts"] = dict(grade_counts)
    statistics["delivery_grade_counts"] = dict(delivery_grade_counts)
    statistics["status_value_counts"] = dict(status_value_counts)
    statistics["operator_versions_unavailable"] = (
        statistics["delivery_records"] if context["operator_versions_source"] == "unavailable" else 0)

    collected = _collect_delivery_metadata(delivered_records, cfg, context)
    statistics["unit_type"] = (collected["unit_type"]
                                if statistics["delivery_records"] else None)
    statistics["unit_type_counts"] = collected["unit_type_counts"]
    statistics["language_pair"] = (collected["language_pair"]
                                    if statistics["delivery_records"] else None)
    statistics["source_types"] = collected["source_types"]
    statistics["model_versions"] = collected["model_versions"]
    statistics["rule_versions"] = collected["rule_versions"]

    log_action(logger, "门禁", "入库门禁完成：输入 %d 条，交付 %d 条，拦截 %d 条，"
                              "待人工 %d 条，重复 id %d 条，元数据冲突 %d 条"
               % (statistics["input_records"], statistics["delivery_records"],
                  statistics["rejected_records"], statistics["waiting_manual_count"],
                  statistics["duplicate_id_records"], statistics["metadata_conflict_records"]))

    # 清单与打包：bytes/sha256 全部从真实落盘文件读取。
    jsonl_meta = file_meta(jsonl_file, records=statistics["delivery_records"])
    tsv_meta = file_meta(tsv_file, records=statistics["delivery_records"])
    reject_meta = file_meta(reject_file, records=statistics["rejected_records"])
    statistics["bytes"] = jsonl_meta["bytes"]
    statistics["sha256"] = jsonl_meta["sha256"]
    statistics["tsv_bytes"] = tsv_meta["bytes"]
    statistics["tsv_sha256"] = tsv_meta["sha256"]
    statistics["reject_bytes"] = reject_meta["bytes"]
    statistics["reject_sha256"] = reject_meta["sha256"]

    manifest = _build_manifest(cfg, unique_id, packaged_at, statistics, collected,
                               [jsonl_meta, tsv_meta, reject_meta], delivered_records)
    write_json_atomic(manifest_file, manifest)
    manifest_meta = file_meta(manifest_file)
    statistics["manifest_bytes"] = manifest_meta["bytes"]
    statistics["manifest_sha256"] = manifest_meta["sha256"]

    member_map = {
        cfg.dataset_jsonl_name: jsonl_file,
        cfg.dataset_tsv_name: tsv_file,
        cfg.manifest_name: manifest_file,
        cfg.reject_name: reject_file,
    }
    if statistics["delivery_records"] == 0 and cfg.empty_delivery_policy == "failed":
        status = "failed"
        message = "没有记录通过入库门禁，未生成交付数据集"
    elif statistics["delivery_records"] == 0:
        # 输入>0 但零交付是严格门禁的正常裁定（默认 partial），与"输入为 0"的失败语义区分
        status = "partial"
        message = "没有记录通过入库门禁，按零交付产出空数据集与清单"
    elif statistics["rejected_records"] > 0:
        status = "partial"
        message = ""
    elif statistics["delivered_waiting_manual_records"] > 0:
        status = "waiting_manual"
        message = ""
    else:
        status = "success"
        message = ""

    if internal_error and not message:
        message = internal_error

    log_action(logger, "输出", "写入交付数据集：%s；异常清单：%s；清单：%s；交付包：%s"
               % (jsonl_file.name, reject_file.name, manifest_file.name, package_file.name))
    # [无血缘改造 2026-10-19] 交付包只含业务产物，不再打包/校验任何血缘文件。
    write_zip_atomic(package_file,
                     [(member_map[name], name) for name in cfg.zip_members],
                     cfg.zip_compression)
    package_meta = file_meta(package_file)
    statistics["package_bytes"] = package_meta["bytes"]
    statistics["package_sha256"] = package_meta["sha256"]
    log_action(logger, "打包", "生成交付包 %s（%d 个成员，%d 字节）；数据集 %d 条 %d 字节；"
                              "清单 sha256=%s；包 sha256=%s"
               % (cfg.package_name, len(cfg.zip_members), statistics["package_bytes"],
                  statistics["delivery_records"], statistics["bytes"],
                  (statistics["manifest_sha256"] or "")[:12],
                  (statistics["package_sha256"] or "")[:12]))

    # 阶段产物：主输出 + 异常清单 + run.log（失败分支不生成，保持 None）。
    stage_zip = storage_uri = None
    if status != "failed":
        stage_zip, storage_uri = finalize_stage_artifacts(
            tdir, OPERATOR_ID, unique_id, jsonl_file, reject_file, log_file)
        log_action(logger, "归档", "阶段产物：%s；存储地址：%s"
                   % (Path(stage_zip).name if stage_zip else "无", storage_uri))
    log_action(logger, "完成", "算子执行完成，状态 %s" % status)
    close_logger(logger)

    return {"status": status,
            "outputFile": str(jsonl_file) if status != "failed" else None,
            "rejectFile": str(reject_file),
            "manifestFile": str(manifest_file), "packageFile": str(package_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子18：元数据构建与数据集打包")
    parser.add_argument("--input", required=True,
                        help="输入句对 JSONL 文件（16_domain_checked.jsonl）")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print("status=%s statistics=%s" % (result["status"], result["statistics"]))
    print("stageZip=%s storageUri=%s" % (result.get("stageZip"), result.get("storageUri")))
    if result["message"]:
        print("message=%s" % result["message"])


if __name__ == "__main__":
    main()
