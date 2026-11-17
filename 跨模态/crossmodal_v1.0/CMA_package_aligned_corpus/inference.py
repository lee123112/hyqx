"""算子 23「压缩交付对齐语料包」编排层：材料门禁 → 流式判定 → ZIP 打包 → 血缘。

表1 #23：默认开启、**必须**、系统功能型、P0；模型列 = ``--``，因此**不存在**
「模型不可用但仍标 model」的伪造降级路径。三种运行状态都由真实证据决定：

- ``success``   材料齐全且 #24 已回写 → 生成**可交付** ZIP 与 manifest；
- ``degraded``  材料不齐或抽检未回写 → 版本**保持待发布**（不拦样本、不伪造可交付）；
- ``failed``    统计守恒失败、全部行不可解析或严格模式下不可交付（显式失败）。

输出目录结构：``OUTPUT_DIR/<uniqueId>/delivery_package/<runId>/``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path
from utils import delivery_handoff
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from pydantic import ValidationError

from utils import archive
from utils import delivery
from utils import io as io_utils
from utils import lineage
from utils import transport
from utils.logger import RunLogger
from utils.schemas import (ConfigError, OperatorConfig, validate_overrides)

__all__ = [
    "InputError",
    "OPERATOR_DIR_NAME",
    # [v4.2] 血缘输出已停用：build_intermediate_lineage / build_stage_payload 不再导出
    "default_config_path",
    "default_output_dir",
    "load_config",
    "main",
    "run",
]

OPERATOR_DIR_NAME = delivery.OPERATOR
ENV_OUTPUT_DIR = "OUTPUT_DIR"
ENV_CONFIG_PATH = "OPERATOR_CONFIG"
CONFIG_RELATIVE = ("configs", "config.yaml")

STATUS_SUCCESS = "success"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

MAX_RUN_DIRS = 999

BADLINE_REASON_KEY = {
    delivery.REASON_JSON_PARSE: "badline_json_parse_error",
    delivery.REASON_NOT_OBJECT: "badline_record_not_object",
    delivery.REASON_ENCODING: "badline_encoding_error",
}

ORIGIN_REASON_KEY = {
    delivery.REASON_MISSING_IDENTIFIER: "origin_missing_identifier",
    delivery.REASON_DUPLICATE_IDENTIFIER: "origin_duplicate_identifier",
    delivery.REASON_UNSAFE_MEDIA_PATH: "origin_unsafe_media_path",
    delivery.REASON_EMPTY_TOKEN: "origin_empty_token",
}

EXCLUDE_REASON_KEY = {delivery.REASON_UPSTREAM_ISOLATED: "excluded_upstream_isolated"}


class InputError(ValueError):
    """输入/配置不可用（路径、文件缺失、YAML 损坏等）。"""


# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #

def operator_dir() -> Path:
    return Path(__file__).resolve().parent


def default_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env).expanduser()
    return operator_dir().joinpath(*CONFIG_RELATIVE)


def default_output_dir() -> Path:
    env = os.environ.get(ENV_OUTPUT_DIR)
    if env:
        return Path(env).expanduser()
    return operator_dir() / "output"


def _read_config_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise InputError("配置文件不存在：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputError("配置文件无法解析：%s（%s）" % (path, exc))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputError("配置文件根节点必须是对象：%s" % path)
    return raw


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """读取并严格校验配置；请求级覆盖只允许业务白名单键。"""
    path = Path(config_path) if config_path else default_config_path()
    raw = _read_config_file(path)
    allowed_overrides = validate_overrides(overrides)
    if allowed_overrides:
        raw = dict(raw)
        raw.update(allowed_overrides)
    try:
        return OperatorConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# 运行目录
# --------------------------------------------------------------------------- #

def _next_run_id(task_dir: Path) -> str:
    """在 ``<task_dir>/<operator>/runNNN`` 下顺延运行编号，绝不覆盖已有运行。"""
    operator_path = task_dir / OPERATOR_DIR_NAME
    existing = []
    if operator_path.is_dir():
        for child in operator_path.iterdir():
            if child.is_dir() and child.name.startswith("run"):
                suffix = child.name[3:]
                if suffix.isdigit():
                    existing.append(int(suffix))
    next_index = (max(existing) + 1) if existing else 1
    if next_index > MAX_RUN_DIRS:
        raise InputError("任务 %s 的运行次数已达上限 %d" % (task_dir.name, MAX_RUN_DIRS))
    return "run%03d" % next_index


# [v4.2] 血缘输出已停用：不再读取上游/历史阶段血缘与阶段上下文（utils/lineage.py 保留存档）。
# def _collect_upstream_lineage(input_dir: Path, task_dir: Path) -> Tuple[List[Dict[str, Any]],
#                                                                        List[Dict[str, Any]]]:
#     """读取上游/历史阶段血缘与阶段上下文（跨阶段衔接依据）。"""
#     nodes: List[Dict[str, Any]] = []
#     contexts: List[Dict[str, Any]] = []
#     for candidate_dir in (input_dir, task_dir):
#         for name in ("lineage.json", "lineage.jsonl"):
#             path = candidate_dir / name
#             if path.is_file():
#                 for node in lineage.read_lineage(path):
#                     nodes.append(node)
#         history = candidate_dir / lineage.LINEAGE_HISTORY_NAME
#         if history.is_file():
#             for node in lineage.read_lineage(history):
#                 nodes.append(node)
#         context_file = candidate_dir / lineage.STAGE_CONTEXT_NAME
#         if context_file.is_file():
#             try:
#                 payload = io_utils.read_json_file(context_file)
#             except ValueError:
#                 payload = None
#             if isinstance(payload, dict):
#                 contexts.append(payload)
#     return nodes, contexts


# --------------------------------------------------------------------------- #
# 媒体尽力搬运
# --------------------------------------------------------------------------- #

def _resolve_ref(ref: str, roots: Sequence[Path]) -> Optional[Path]:
    for root in roots:
        try:
            candidate = io_utils.resolve_media(root, ref)
        except io_utils.UnsupportedMediaPath:
            continue
        if candidate.is_file():
            return candidate
    return None


def _copy_into_run_dir(run_dir: Path, ref: str, source: Path) -> bool:
    """把媒体复制到 run 目录并保持声明相对路径；返回是否复制成功。"""
    import shutil

    name = ref.split("/", 1)[0]
    if name in set(delivery.OWN_SIDECARS) | {transport.MANIFEST_NAME}:
        return False
    try:
        target = io_utils.resolve_media(run_dir, ref)
    except io_utils.UnsupportedMediaPath:               # pragma: no cover - 已过滤
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    try:
        shutil.copyfile(str(source), str(temp))
        os.replace(str(temp), str(target))
    except OSError:                                     # pragma: no cover
        if temp.exists():
            temp.unlink()
        return False
    return True


def _collect_media(refs: Sequence[str], roots: Sequence[Path],
                   max_media_bytes: int) -> Tuple[List[Tuple[str, Path, int]],
                                                  int, int]:
    """返回 (可搬运成员, 缺失唯一引用数, 超过单文件上限数)。"""
    carried: List[Tuple[str, Path, int]] = []
    missing = 0
    oversized = 0
    for ref in refs:
        source = _resolve_ref(ref, roots)
        if source is None:
            missing += 1
            continue
        size = source.stat().st_size
        if size > max_media_bytes:
            oversized += 1
            continue
        carried.append((ref, source, size))
    return carried, missing, oversized


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(*, input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True) -> Dict[str, Any]:
    """执行一次完整打包判定。返回结果字典（供 CLI 与 HTTP 层复用）。"""
    task_id = io_utils.validate_unique_id(unique_id)
    config = load_config(config_path, overrides)
    root = Path(output_root) if output_root else default_output_dir()
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    task_dir = (root / task_id).resolve()

    input_path = Path(input_file).expanduser()
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path
    input_path = input_path.resolve()
    if not allow_external_input and task_dir not in input_path.parents:
        raise InputError("inputFile 必须位于任务目录内：%s" % input_path)
    if input_path.is_dir():
        raise InputError("inputFile 必须是文件：%s" % input_path)
    if not input_path.is_file():
        raise InputError("inputFile 不存在：%s" % input_path)

    started_at = lineage.now_iso()
    with io_utils.TaskLock(root, task_id):
        run_id = _next_run_id(task_dir)
        run_dir = task_dir / OPERATOR_DIR_NAME / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        with RunLogger(run_dir / delivery.LOG_NAME) as logger:
            result = _process(input_path=input_path, task_dir=task_dir,
                              run_dir=run_dir, run_id=run_id, task_id=task_id,
                              config=config, started_at=started_at, logger=logger)
    return result


def _process(*, input_path: Path, task_dir: Path, run_dir: Path, run_id: str,
             task_id: str, config: OperatorConfig, started_at: str,
             logger: RunLogger) -> Dict[str, Any]:
    main_path = run_dir / delivery.MAIN_NAME
    excluded_path = run_dir / delivery.EXCLUDED_NAME
    materials_path = run_dir / delivery.MATERIALS_NAME
    rejects_path = run_dir / delivery.REJECTS_NAME
    # [v4.2] 血缘输出已停用：不再生成本地血缘/阶段上下文文件
    # lineage_path = run_dir / lineage.LINEAGE_NAME
    # context_path = run_dir / lineage.STAGE_CONTEXT_NAME

    package_root = input_path.parent
    task_input_dir = task_dir / "input"
    materials_root = package_root
    if (task_input_dir.resolve() in input_path.resolve().parents
            and package_root.resolve() != task_input_dir.resolve()
            and (task_input_dir / transport.MANIFEST_NAME).is_file()
            and "material_handoff" in transport.load_manifest(task_input_dir)):
        # The package manifest, not the directory containing a nested entry,
        # owns the material binding. Local names cannot replace its evidence.
        materials_root = task_input_dir
    materials = delivery.inspect_materials(materials_root, config=config,
                                           entry_path=input_path)
    # 阶段内后续算子传入上一节点的中间文件时，材料仍来自任务输入包（只读回退一次）
    if (materials["missing"] and task_input_dir.is_dir()
            and task_input_dir.resolve() != materials_root.resolve()):
        fallback = delivery.inspect_materials(task_input_dir, config=config,
                                              entry_path=input_path)
        if len(fallback["found"]) > len(materials["found"]):
            fallback["fallbackRoot"] = str(task_input_dir)
            materials = fallback
            materials_root = task_input_dir
    isolation_role = materials["roles"].get(delivery.MATERIAL_ISOLATION_LIST) or {}
    isolation_ids = set(isolation_role.get("sample_ids") or [])
    version_role = materials["roles"].get(delivery.MATERIAL_VERSION_NOTES) or {}
    enabled_routes = list(version_role.get("routes") or []) \
        if version_role.get("status") == "found" else []
    logger.event("materials", "材料发现完成", found=len(materials["found"]),
                 missing="|".join(materials["missing"]) or "none",
                 invalid="|".join(materials["invalid"]) or "none",
                 isolated=len(isolation_ids))

    stats: Dict[str, Any] = {
        "input_records": 0, "valid_records": 0, "blank_lines": 0, "bad_lines": 0,
        "packaged_records": 0, "excluded_isolated": 0, "origin_quarantined": 0,
        "rejects_entries": 0,
        "media_referenced": 0, "media_distinct": 0, "media_carried": 0,
        "media_missing": 0, "media_oversized": 0, "media_skipped_by_config": 0,
        "media_copied_to_run": 0, "external_references": 0,
        "materials_found": list(materials["found"]),
        "materials_missing": list(materials["missing"]),
        "materials_invalid": list(materials["invalid"]),
        "materials_root": str(materials_root),
        "materials_fallback_root": bool(materials.get("fallbackRoot")),
        "materials_found_count": len(materials["found"]),
        "materials_required_count": len(delivery.MATERIAL_ROLES),
        "deliverable": False, "version_state": delivery.VERSION_STATE_PENDING,
        "status_label": config.status_before_review,
        "zip_entries": 0, "zip_bytes": 0, "zip_sha256": None,
        "package_name": delivery.PACKAGE_PENDING_NAME,
        "applicable_records": 0, "not_applicable_records": 0,
        "applicability_unknown_records": 0,
        "enabled_routes": enabled_routes,
        "empty_input": False, "encoding_error": False, "file_read_incomplete": False,
        "conservation_ok": True, "conservation_failures": [],
        "model": None, "models_available": False, "rule_based": True,
        "degraded": False, "enabled": bool(config.enabled),
        "archive_format": config.archive_format,
        "exclude_isolated": bool(config.exclude_isolated),
        "include_media": bool(config.include_media),
        "require_token": bool(config.require_token),
        "require_deliverable": bool(config.require_deliverable),
        "isolation_list_entries": int(isolation_role.get("entries") or 0),
    }
    origin_by_reason: Dict[str, int] = {}
    excluded_by_reason: Dict[str, int] = {}
    bad_line_by_reason: Dict[str, int] = {}
    seen_identifiers: set = set()
    media_refs_ordered: List[str] = []
    external_refs_ordered: List[str] = []

    logger.event("run_start", "开始压缩交付对齐语料包",
                 inputFile=input_path.name, uniqueId=task_id, runId=run_id,
                 archiveFormat=config.archive_format,
                 requireDeliverable=config.require_deliverable)

    encoding_error = False
    with io_utils.JsonlWriter(main_path) as main_writer, \
            io_utils.JsonlWriter(excluded_path) as excluded_writer, \
            io_utils.JsonlWriter(rejects_path) as rejects_writer:
        line_iter = io_utils.iter_decoded_lines(input_path)
        line_no = 0
        while True:
            try:
                raw_line = next(line_iter)
            except StopIteration:
                break
            except UnicodeDecodeError as exc:
                encoding_error = True
                stats["encoding_error"] = True
                stats["file_read_incomplete"] = True
                stats["input_records"] += 1
                stats["bad_lines"] += 1
                stats["rejects_entries"] += 1
                _bump(bad_line_by_reason, delivery.REASON_ENCODING)
                rejects_writer.write(delivery.make_reject_entry(
                    line=line_no + 1, reason=delivery.REASON_ENCODING,
                    detail="文件不是 UTF-8 编码，已停止解析：%s" % exc.reason,
                    detected_at=lineage.now_iso()))
                break
            line_no += 1
            text = raw_line.strip()
            if not text:
                stats["blank_lines"] += 1
                continue
            stats["input_records"] += 1
            now = lineage.now_iso()

            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                stats["bad_lines"] += 1
                stats["rejects_entries"] += 1
                _bump(bad_line_by_reason, delivery.REASON_JSON_PARSE)
                rejects_writer.write(delivery.make_reject_entry(
                    line=line_no, reason=delivery.REASON_JSON_PARSE,
                    detail="JSON 解析失败，进入原始隔离区", detected_at=now,
                    raw_text=text))
                continue

            if not isinstance(payload, dict):
                stats["bad_lines"] += 1
                stats["rejects_entries"] += 1
                _bump(bad_line_by_reason, delivery.REASON_NOT_OBJECT)
                rejects_writer.write(delivery.make_reject_entry(
                    line=line_no, reason=delivery.REASON_NOT_OBJECT,
                    detail="记录不是 JSON 对象，进入原始隔离区", detected_at=now,
                    raw_text=text))
                continue

            stats["valid_records"] += 1
            spec = delivery.resolve_record(payload)
            identifier = spec["identifier"]
            if identifier and identifier in seen_identifiers:
                stats["origin_quarantined"] += 1
                stats["rejects_entries"] += 1
                _bump(origin_by_reason, delivery.REASON_DUPLICATE_IDENTIFIER)
                rejects_writer.write(delivery.make_reject_entry(
                    line=line_no, reason=delivery.REASON_DUPLICATE_IDENTIFIER,
                    detail="标识与前面的记录重复（%s）" % identifier,
                    detected_at=now, identifier=identifier))
                continue
            if identifier:
                seen_identifiers.add(identifier)

            outcome = delivery.evaluate_record(
                payload, isolation_ids=isolation_ids, event_time=now, config=config,
                line=line_no, enabled_routes=enabled_routes)

            if outcome["action"] == delivery.ACTION_PACKAGE:
                main_writer.write(outcome["output"])
                stats["packaged_records"] += 1
                applicability = outcome.get("applicability", "unknown")
                applicability_key = {
                    "applicable": "applicable_records",
                    "not_applicable": "not_applicable_records",
                    "unknown": "applicability_unknown_records",
                }[applicability]
                stats[applicability_key] += 1
                media_refs_ordered.extend(spec["media_refs"])
                external_refs_ordered.extend(spec["external_refs"])
                stats["media_referenced"] += len(spec["media_refs"])
                stats["external_references"] += len(spec["external_refs"])
            elif outcome["action"] == delivery.ACTION_EXCLUDE:
                entry = outcome["entry"]
                entry["line"] = line_no
                excluded_writer.write(entry)
                stats["excluded_isolated"] += 1
                _bump(excluded_by_reason, delivery.REASON_UPSTREAM_ISOLATED)
            else:
                rejects_writer.write(delivery.make_ledger_entry(
                    line=line_no, payload=payload, action=delivery.ACTION_ORIGIN,
                    label=outcome["label"], destination=delivery.DEST_ORIGIN,
                    reason_code=outcome["reason"], event_time=now,
                    detail=outcome["detail"],
                    max_embedded_bytes=config.max_embedded_record_bytes))
                stats["origin_quarantined"] += 1
                stats["rejects_entries"] += 1
                _bump(origin_by_reason, outcome["reason"])

    distinct_refs = _unique(media_refs_ordered)
    stats["media_distinct"] = len(distinct_refs)
    stats["external_references"] = len(_unique(external_refs_ordered))
    stats["external_reference_list"] = external_refs_ordered[:50]

    media_roots = _unique_paths([package_root, materials_root, task_input_dir, task_dir])
    if config.include_media:
        carried, missing, oversized = _collect_media(
            distinct_refs, media_roots, config.max_media_bytes)
    else:
        # 表1：原文件以引用/清单形式承载时，本算子不复制媒体（大包交工程侧 OSS）
        carried, missing, oversized = [], 0, 0
        stats["media_skipped_by_config"] = len(distinct_refs)
    stats["media_missing"] = missing
    stats["media_oversized"] = oversized
    # demo 口径：把随包携带的媒体复制到 run 目录（保持声明的相对路径），
    # 使运行目录自包含、可整体搬运；大包场景由工程侧 OSS/分片承载。
    stats["media_copied_to_run"] = sum(
        1 for ref, source, _size in carried
        if _copy_into_run_dir(run_dir, ref, source))

    gate = delivery.evaluate_deliverable(materials, packaged_records=stats["packaged_records"],
                                         config=config)
    stats["deliverable"] = gate["deliverable"]
    stats["version_state"] = gate["version_state"]
    stats["status_label"] = gate["status_label"]
    stats["deliverable_requirements"] = dict(gate["requirements"])
    stats["deliverable_missing_requirements"] = list(gate["missing_requirements"])
    stats["materials_complete"] = gate["materials_complete"]
    stats["package_name"] = gate["package_name"]

    if gate["version_state"] == delivery.VERSION_STATE_DELIVERABLE:
        _patch_decision(main_path, delivery.VERSION_STATE_DELIVERABLE)

    package_path = run_dir / gate["package_name"]
    members = _build_members(main_path=main_path, materials=materials,
                             carried=carried if config.include_media else [],
                             stats=stats, gate=gate, task_id=task_id, run_id=run_id,
                             config=config)
    package_report = archive.build_package(package_path, members,
                                           timestamp=_zip_timestamp(started_at))
    stats["media_carried"] = len(carried) if config.include_media else 0
    stats["zip_entries"] = package_report["entries"]
    stats["zip_bytes"] = package_report["bytes"]
    stats["zip_sha256"] = package_report["sha256"]
    stats["uncompressed_bytes"] = package_report["uncompressed_bytes"]

    stats["empty_input"] = stats["input_records"] == 0
    stats["bad_line_by_reason"] = bad_line_by_reason
    stats["origin_by_reason"] = origin_by_reason
    stats["excluded_by_reason"] = excluded_by_reason
    for mapping, bucket in ((ORIGIN_REASON_KEY, origin_by_reason),
                            (EXCLUDE_REASON_KEY, excluded_by_reason),
                            (BADLINE_REASON_KEY, bad_line_by_reason)):
        for reason, key in mapping.items():
            stats[key] = bucket.get(reason, 0)

    stats["train_records_in_package"] = io_utils.count_jsonl_records(main_path)
    stats["conservation_ok"], stats["conservation_failures"] = _check_conservation(stats)
    status = _resolve_status(stats, config)
    stats["degraded"] = status == STATUS_DEGRADED

    io_utils.atomic_write_json(materials_path, _materials_payload(materials, gate, config))

    finished_at = lineage.now_iso()
    artifacts = _build_artifacts(run_dir, package_name=gate["package_name"],
                                 media=[{"path": ref, "role": "media",
                                         "size": size}
                                        for ref, _source, size in carried
                                        if config.include_media])
    # [v4.2] 血缘输出已停用：上游血缘归集、血缘节点构造、血缘文件与阶段上下文写入
    # 全部停用（utils/lineage.py 保留存档，不再调用）。
    # upstream_nodes, prior_contexts = _collect_upstream_lineage(materials_root, task_dir)
    # node = _build_lineage_node(task_id=task_id, run_id=run_id, run_dir=run_dir,
    #                            input_path=input_path, config=config, stats=stats,
    #                            status=status, started_at=started_at,
    #                            finished_at=finished_at, artifacts=artifacts,
    #                            output_file=main_path, upstream=upstream_nodes)
    # stage_nodes = [dict(item) for item in upstream_nodes] + [node]
    # lineage.write_lineage(lineage_path, stage_nodes)
    #
    # context = {"format": "crossmodal-stage-context-v1", "stageId": config.stage_id,
    #            "stage_id": config.stage_id, "uniqueId": task_id, "runId": run_id,
    #            "operator": delivery.OPERATOR,
    #            "operatorVersion": delivery.OPERATOR_VERSION,
    #            "formalOperatorNumber": delivery.OPERATOR_NUMBER,
    #            "entrypoint": delivery.MAIN_NAME,
    #            "inputFile": str(input_path), "runDir": str(run_dir),
    #            "status": status, "startedAt": started_at, "finishedAt": finished_at,
    #            "versionState": gate["version_state"],
    #            "deliverable": gate["deliverable"],
    #            "config": lineage.config_snapshot(config),
    #            "statistics": lineage.statistics_snapshot(stats),
    #            "artifacts": artifacts, "priorStageContexts": prior_contexts,
    #            "priorLineage": [{"operator": item.get("operator"),
    #                              "operator_version": item.get("operator_version"),
    #                              "status": item.get("status"),
    #                              "stageId": item.get("stageId"),
    #                              "finishedAt": item.get("finishedAt")}
    #                             for item in upstream_nodes]}
    # lineage.write_stage_context(context_path, context)

    manifest = transport.build_manifest(
        delivery.MAIN_NAME,
        [{"path": item["path"], "role": item["role"], "size": item.get("size")}
         for item in artifacts]
        + [{"path": transport.MANIFEST_NAME, "role": "sidecar"}])
    manifest[delivery_handoff.KEY] = delivery_handoff.create(run_dir, package_path)
    transport.write_manifest(run_dir, manifest)

    logger.event("run_finish", "打包完成", status=status,
                 versionState=gate["version_state"],
                 input=stats["input_records"],
                 packaged=stats["packaged_records"],
                 excluded=stats["excluded_isolated"],
                 origin=stats["origin_quarantined"],
                 zipEntries=stats["zip_entries"],
                 mediaCarried=stats["media_carried"],
                 conservationOk=stats["conservation_ok"])

    return {
        "status": status,
        "message": _status_message(status, stats, gate),
        "operator": delivery.OPERATOR,
        "operatorVersion": delivery.OPERATOR_VERSION,
        "formalOperatorNumber": delivery.OPERATOR_NUMBER,
        "stageId": config.stage_id,
        "uniqueId": task_id,
        "runId": run_id,
        "taskDir": str(task_dir),
        "runDir": str(run_dir),
        "inputFile": str(input_path),
        "entrypoint": delivery.MAIN_NAME,
        "outputFile": str(main_path),
        "excludedFile": str(excluded_path),
        "materialsFile": str(materials_path),
        "rejectsFile": str(rejects_path),
        "packagePath": str(package_path),
        "packageName": gate["package_name"],
        "versionState": gate["version_state"],
        "deliverable": gate["deliverable"],
        "statusLabel": gate["status_label"],
        "materials": materials,
        "deliverableGate": gate,
        # [v4.2] 血缘输出已停用：run() 返回不再包含 lineageFile/contextFile/
        # stageLineage/upstreamLineage 等血缘键。
        "manifestPath": str(run_dir / transport.MANIFEST_NAME),
        "logFile": str(run_dir / delivery.LOG_NAME),
        "artifacts": artifacts,
        "statistics": stats,
        "model": None,
        "modelsAvailable": False,
        "ruleBased": True,
        "transport": {"format": config.transport.format,
                      "maxResponseBytes": config.transport.max_response_bytes},
        "config": lineage.config_snapshot(config),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "entrypointNames": [delivery.MAIN_NAME, delivery.EXCLUDED_NAME,
                            delivery.MATERIALS_NAME, delivery.REJECTS_NAME],
    }


def _unique_paths(values: Sequence[Path]) -> List[Path]:
    result: List[Path] = []
    seen = set()
    for item in values:
        try:
            resolved = Path(item).resolve()
        except OSError:                                # pragma: no cover
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _unique(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _bump(counter: Dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _zip_timestamp(started_at: str) -> Tuple[int, int, int, int, int, int]:
    """用本次运行开始时间做 ZIP 时间戳（同输入同输出可复现，且不早于 1980）。"""
    text = (started_at or "").replace("Z", "+00:00")
    try:
        from datetime import datetime

        moment = datetime.fromisoformat(text)
        stamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute,
                 moment.second)
        if stamp[0] < 1980:
            return (1980, 1, 1, 0, 0, 0)
        return stamp
    except ValueError:                                 # pragma: no cover - 兜底
        return (2026, 1, 1, 0, 0, 0)


def _patch_decision(main_path: Path, version_state: str) -> None:
    """把主清单里每条记录的 ``decision`` 改写成最终版本状态（流式、原子替换）。"""
    with io_utils.JsonlWriter(main_path) as writer:
        for line in io_utils.iter_decoded_lines(main_path):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):   # pragma: no cover - 自身产物
                writer.write(line)
                continue
            if not isinstance(payload, dict):
                writer.write(payload)
                continue
            node = payload.get(delivery.NAMESPACE)
            if isinstance(node, dict):
                node["decision"] = version_state
            processing = payload.get("processing")
            if isinstance(processing, dict) and isinstance(processing.get("23"), dict):
                processing["23"]["decision"] = version_state
            writer.write(payload)


def _build_members(*, main_path: Path, materials: Dict[str, Any],
                   carried: Sequence[Tuple[str, Path, int]],
                   stats: Dict[str, Any], gate: Dict[str, Any], task_id: str,
                   run_id: str, config: OperatorConfig) -> List[Tuple[str, bytes]]:
    """构造交付包成员（成员路径固定、顺序确定）。"""
    members: List[Tuple[str, bytes]] = [
        (delivery.TRAIN_MEMBER, main_path.read_bytes())]

    for role in (delivery.MATERIAL_QUALITY_REPORT, delivery.MATERIAL_SAMPLING_CONCLUSION,
                 delivery.MATERIAL_ISOLATION_LIST, delivery.MATERIAL_VERSION_NOTES):
        role_entry = materials["roles"].get(role) or {}
        member = delivery.package_member_for_role(role)
        if role_entry.get("status") != "found":
            continue
        if role == delivery.MATERIAL_ISOLATION_LIST and role_entry.get("components"):
            parts = []
            for component in role_entry["components"]:
                raw = Path(component["absolutePath"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != component["sha256"]:
                    raise InputError("隔离材料在归档前发生变化")
                if raw:
                    parts.append(raw if raw.endswith(b"\n") else raw + b"\n")
            members.append((member, b"".join(parts)))
            continue
        source_text = role_entry.get("absolutePath") or role_entry.get("path")
        if not source_text:
            continue
        source = Path(source_text)
        if source.is_file():
            raw = source.read_bytes()
            if (role_entry.get("source") == "material_handoff"
                    and hashlib.sha256(raw).hexdigest() != role_entry["sha256"]):
                raise InputError("交付材料在归档前发生变化")
            members.append((member, raw))

    version_notes_role = materials["roles"].get(delivery.MATERIAL_VERSION_NOTES) or {}
    version_payload = {
        "format": "crossmodal-delivery-version-v1",
        "operator": delivery.OPERATOR,
        "operator_number": delivery.OPERATOR_NUMBER,
        "operator_version": delivery.OPERATOR_VERSION,
        "source_reference": delivery.SOURCE_REFERENCE,
        "uniqueId": task_id,
        "runId": run_id,
        "stage_id": config.stage_id,
        "created_at": lineage.now_iso(),
        "dataset_version": version_notes_role.get("dataset_version"),
        "enabled_routes": list(version_notes_role.get("routes") or []),
        "annotation_granularity": version_notes_role.get("granularity"),
        "status": gate["status_label"],
        "version_state": gate["version_state"],
        "status_before_review": config.status_before_review,
        "status_after_review": config.status_after_review,
        "packaged_records": stats["packaged_records"],
        "excluded_isolated": stats["excluded_isolated"],
        "missing_requirements": list(gate["missing_requirements"]),
        "isolation_excluded_from_package": True,
        "archive_format": config.archive_format,
    }
    members.append((delivery.PACKAGE_VERSION_MEMBER,
                    _json_bytes(version_payload)))

    for ref, source, _size in carried:
        members.append((ref, source.read_bytes()))

    files = [{"path": name, "role": transport.role_for(name),
              "size": len(payload), "sha256": archive.sha256_bytes(payload)}
             for name, payload in members]
    package_manifest = {
        "format": delivery.PACKAGE_FORMAT,
        "package_version": delivery.OPERATOR_VERSION,
        "operator": delivery.OPERATOR,
        "operator_number": delivery.OPERATOR_NUMBER,
        "operator_version": delivery.OPERATOR_VERSION,
        "source_reference": delivery.SOURCE_REFERENCE,
        "uniqueId": task_id,
        "runId": run_id,
        "stage_id": config.stage_id,
        "created_at": lineage.now_iso(),
        "entrypoint": delivery.TRAIN_MEMBER,
        "version_state": gate["version_state"],
        "status": gate["status_label"],
        "deliverable": gate["deliverable"],
        "requirements": dict(gate["requirements"]),
        "missing_requirements": list(gate["missing_requirements"]),
        "materials": {role: {"status": (materials["roles"].get(role) or {}).get("status"),
                             "relative": (materials["roles"].get(role) or {}).get("relative"),
                             "written_back": (materials["roles"].get(role) or {}).get("written_back")}
                      for role in delivery.MATERIAL_ROLES},
        "statistics": {"input_records": stats["input_records"],
                       "valid_records": stats["valid_records"],
                       "packaged_records": stats["packaged_records"],
                       "excluded_isolated": stats["excluded_isolated"],
                       "origin_quarantined": stats["origin_quarantined"],
                       "bad_lines": stats["bad_lines"],
                       "media_carried": stats["media_carried"],
                       "media_missing": stats["media_missing"]},
        "externalReferences": list(stats.get("external_reference_list") or []),
        "files": files,
        "notes": ("隔离样本不进入交付包；包内文件路径均为包内相对路径；"
                  "大包由工程侧 OSS/分片承载，本算子不做 OSS 上传。"),
    }
    members.append((delivery.PACKAGE_MANIFEST_MEMBER, _json_bytes(package_manifest)))
    return members


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _materials_payload(materials: Dict[str, Any], gate: Dict[str, Any],
                       config: OperatorConfig) -> Dict[str, Any]:
    roles = {role: {key: value for key, value in (materials["roles"].get(role) or {}).items()
                    if key not in {"sample_ids"}}
             for role in delivery.MATERIAL_ROLES}
    for role, entry in roles.items():
        if role == delivery.MATERIAL_ISOLATION_LIST:
            source = materials["roles"].get(role) or {}
            entry["sample_ids_count"] = len(source.get("sample_ids") or [])
    return {"format": delivery.MATERIALS_FORMAT,
            "operator": delivery.OPERATOR,
            "operator_number": delivery.OPERATOR_NUMBER,
            "operator_version": delivery.OPERATOR_VERSION,
            "entry": materials["entry"],
            "root": materials["root"],
            "roles": roles,
            "found": list(materials["found"]),
            "missing": list(materials["missing"]),
            "invalid": list(materials["invalid"]),
            "recursive_scan_used": materials["recursive_scan_used"],
            "requirements": dict(gate["requirements"]),
            "required_roles": list(delivery.MATERIAL_ROLES),
            "deliverable": gate["deliverable"],
            "version_state": gate["version_state"],
            "status": gate["status_label"],
            "status_before_review": config.status_before_review,
            "status_after_review": config.status_after_review,
            "missing_requirements": list(gate["missing_requirements"]),
            "notes": ("材料不齐或抽检未回写时版本保持待发布（不拦样本）；"
                      "已在隔离区的样本不进入语料包。")}


def _resolve_status(stats: Dict[str, Any], config: OperatorConfig) -> str:
    if not stats.get("conservation_ok", True):
        return STATUS_FAILED
    if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
        return STATUS_FAILED
    if config.strict_delivery and not stats["deliverable"]:
        return STATUS_FAILED
    if not stats["deliverable"]:
        return STATUS_DEGRADED
    return STATUS_SUCCESS


def _check_conservation(stats: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """核对「输入 = 各类去向」以及台账/包内计数守恒等式。"""
    equality_checks = [
        ("input_records==valid_records+bad_lines",
         stats["input_records"], stats["valid_records"] + stats["bad_lines"]),
        ("valid_records==packaged+excluded+origin",
         stats["valid_records"], stats["packaged_records"]
         + stats["excluded_isolated"] + stats["origin_quarantined"]),
        ("bad_lines==sum(bad_line_by_reason)",
         stats["bad_lines"], sum(stats["bad_line_by_reason"].values())),
        ("origin_quarantined==sum(origin_by_reason)",
         stats["origin_quarantined"], sum(stats["origin_by_reason"].values())),
        ("excluded_isolated==sum(excluded_by_reason)",
         stats["excluded_isolated"], sum(stats["excluded_by_reason"].values())),
        ("rejects_entries==bad_lines+origin_quarantined",
         stats["rejects_entries"], stats["bad_lines"] + stats["origin_quarantined"]),
        ("train_records_in_package==packaged_records",
         stats["train_records_in_package"], stats["packaged_records"]),
        ("applicable+not_applicable+unknown==packaged_records",
         stats["applicable_records"] + stats["not_applicable_records"]
         + stats["applicability_unknown_records"], stats["packaged_records"]),
        ("media_carried+missing+oversized+skipped==media_distinct",
         stats["media_carried"] + stats["media_missing"] + stats["media_oversized"]
         + stats["media_skipped_by_config"], stats["media_distinct"]),
        ("materials_found+missing+invalid==required",
         stats["materials_found_count"] + len(stats["materials_missing"])
         + len(stats["materials_invalid"]), stats["materials_required_count"]),
    ]
    failures = ["%s（实际 %s != 期望 %s）" % (name, left, right)
                for name, left, right in equality_checks if left != right]
    if stats["zip_entries"] < 1:
        failures.append("zip_entries>=1（交付包至少含训练文件）")
    if stats["packaged_records"] < 0 or stats["excluded_isolated"] < 0:
        failures.append("出现负数统计，说明计数逻辑异常")
    return (not failures), failures


def _build_artifacts(run_dir: Path, *, package_name: str,
                     media: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for name, role in ((delivery.MAIN_NAME, "records"),
                       (delivery.EXCLUDED_NAME, "records"),
                       (delivery.MATERIALS_NAME, "sidecar"),
                       (delivery.REJECTS_NAME, "records"),
                       (package_name, "package"),
                       (delivery.LOG_NAME, "log"),
                       (lineage.LINEAGE_NAME, "sidecar"),
                       (lineage.STAGE_CONTEXT_NAME, "sidecar")):
        path = run_dir / name
        if path.is_file():
            entries.append({"path": name, "role": role, "size": path.stat().st_size})
    for item in media:
        entries.append({"path": item["path"], "role": item["role"],
                        "size": item.get("size")})
    return entries


def _build_lineage_node(*, task_id: str, run_id: str, run_dir: Path, input_path: Path,
                        config: OperatorConfig, stats: Dict[str, Any], status: str,
                        started_at: str, finished_at: str,
                        artifacts: Sequence[Dict[str, Any]],
                        output_file: Path,
                        upstream: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "operator": delivery.OPERATOR,
        "operator_version": delivery.OPERATOR_VERSION,
        "formalOperatorNumber": delivery.OPERATOR_NUMBER,
        "stageId": config.stage_id,
        "uniqueId": task_id,
        "runId": run_id,
        "runDir": str(run_dir),
        "inputFile": str(input_path),
        "outputFile": str(output_file),
        "outputArtifact": delivery.MAIN_NAME,
        "packageArtifact": stats.get("package_name"),
        "status": status,
        "versionState": stats.get("version_state"),
        "deliverable": bool(stats.get("deliverable")),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": lineage.config_snapshot(config),
        "statistics": lineage.statistics_snapshot(stats),
        "artifacts": list(artifacts),
        "upstreamNodes": len(upstream),
        "model": None,
        "modelsAvailable": False,
        "ruleBased": True,
        "lineageFormat": "crossmodal-lineage-v1",
    }


def _strip_output_file(node: Dict[str, Any]) -> Dict[str, Any]:
    stripped = dict(node)
    stripped.pop("outputFile", None)
    stripped.pop("runDir", None)
    return stripped


def _status_message(status: str, stats: Dict[str, Any], gate: Dict[str, Any]) -> str:
    if status == STATUS_FAILED:
        if stats.get("conservation_ok") is False:
            return "统计守恒校验失败：" + "；".join(stats["conservation_failures"])
        if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
            return "全部 %d 行均无法解析，未产生任何可用记录" % stats["bad_lines"]
        return ("严格模式（require_deliverable=true）下材料不齐或抽检未回写，"
                "按配置显式失败：%s" % "；".join(gate["missing_requirements"]))
    if stats["deliverable"]:
        return ("已生成可交付包 %s：训练文件 %d 条；排除隔离样本 %d 条；"
                "原始隔离 %d 条；包内 %d 个成员"
                % (stats["package_name"], stats["packaged_records"],
                   stats["excluded_isolated"], stats["origin_quarantined"],
                   stats["zip_entries"]))
    return ("版本保持待发布（%s）：训练文件 %d 条；排除隔离样本 %d 条；"
            "原始隔离 %d 条；待补/未回写：%s"
            % (stats["package_name"], stats["packaged_records"],
               stats["excluded_isolated"], stats["origin_quarantined"],
               "；".join(gate["missing_requirements"]) or "材料齐全"))


# --------------------------------------------------------------------------- #
# 阶段血缘形态
# --------------------------------------------------------------------------- #
#
# [v4.2] 血缘输出已停用：lineageFile 返回形态不再使用（响应统一见 app.py §3 改造）。
#
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """阶段中间响应：只返回当前节点的 ``lineageFile`` 对象（含临时 outputFile）。"""
#     node = dict(result["stageLineage"][-1])
#     node["outputFile"] = result["outputFile"]
#     node["runDir"] = result["runDir"]
#     return node
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末响应：完整阶段血缘（每个节点都不含临时 outputFile）。"""
#     return [_strip_output_file(node) for node in result["stageLineage"]]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 23 压缩交付对齐语料包（系统功能型，无模型）")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径（#22 Token 清单）")
    parser.add_argument("--unique-id", required=True, help="任务标识")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="输出根目录")
    parser.add_argument("--override", default=None, help="请求级配置覆盖（JSON 对象）")
    parser.add_argument("--restrict-to-task", action="store_true",
                        help="只允许输入位于任务目录内（默认允许任意本地路径）")
    parser.add_argument("--is-last-operator", action="store_true",
                        help="按阶段末响应形态打印 data（文件对象 + 阶段血缘）")
    return parser


def _force_utf8_stdio() -> None:
    """把 CLI 的 stdout/stderr 固定为 UTF-8。

    Windows 控制台默认代码页（cp936）会让中文 JSON 变成非 UTF-8 字节，
    工程侧重定向取回时会解码失败；这里强制 UTF-8，保证 stdout 可直接解析。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="\n")
        except (ValueError, OSError):                  # pragma: no cover - 老解释器
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdio()
    args = _build_parser().parse_args(argv)
    overrides: Optional[Dict[str, Any]] = None
    if args.override:
        try:
            overrides = json.loads(args.override)
        except json.JSONDecodeError as exc:
            print(json.dumps({"code": 1, "msg": "override 不是合法 JSON：%s" % exc,
                              "data": {}}, ensure_ascii=False))
            return 2
        if not isinstance(overrides, dict):
            print(json.dumps({"code": 1, "msg": "override 必须是 JSON 对象",
                              "data": {}}, ensure_ascii=False))
            return 2
    try:
        result = run(input_file=args.input, unique_id=args.unique_id,
                     config_path=args.config, output_root=args.output_root,
                     overrides=overrides,
                     allow_external_input=not args.restrict_to_task)
    except (InputError, ConfigError, ValueError) as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {"errorType": type(exc).__name__}},
                         ensure_ascii=False))
        return 2
    except io_utils.TaskBusyError as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {"errorType": "TaskBusy"}},
                         ensure_ascii=False))
        return 2
    except archive.ArchiveError as exc:                # pragma: no cover - 罕见
        print(json.dumps({"code": 1, "msg": str(exc),
                          "data": {"errorType": "ArchiveError"}}, ensure_ascii=False))
        return 1

    if args.is_last_operator:
        run_dir = Path(result["runDir"])
        paths = [delivery.MAIN_NAME]
        paths.extend(item["path"] for item in result["artifacts"])
        paths.append(transport.MANIFEST_NAME)
        file_object = transport.pack_file_object(
            run_dir, delivery.MAIN_NAME, paths,
            max_response_bytes=result["transport"]["maxResponseBytes"])
        data: Dict[str, Any] = {"outputFile": file_object}
        # [v4.2] 血缘输出已停用：CLI 阶段末形态不再回传 lineageFile
    else:
        data = result
    refused = result.get("deliverable", (result.get("statistics") or {}).get("deliverable")) is False
    payload = {"code": 1 if refused else 0, "msg": result["message"], "data": data}
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if refused or result["status"] == STATUS_FAILED else 0


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(main())
