"""算子 22「生成跨模态Token」编排层：校验 → 流式判定 → 分路线产物 → 血缘。

表1 v4.1 #22（5 样本构建 / 系统功能型 / 模型列 ``--`` / 默认开启 / 必须 / P0）：
- 输入 = 候选池中已有质量结论且不在隔离区的样本；
- 没有质量结论 → 不拦样本、留在产线、不生成 Token；
- 隔离区样本不进入样本文件；未开启路线不产出对应文件；
- 文本模态位置写成 ``<image>`` / ``<audio>`` / ``<video>``；
- 输出 Token 文件与样本文件；**原文件仅保留对象存储链接**；
- 本算子**不压缩、不改版本状态**。

输出目录结构：``OUTPUT_DIR/<uniqueId>/crossmodal_token/<runId>/``
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from pydantic import ValidationError

from utils import io as io_utils
from utils import lineage, names, tokens, transport, upstream
from utils.logger import RunLogger
from utils.schemas import ConfigError, OperatorConfig, validate_overrides

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

OPERATOR_DIR_NAME = names.OPERATOR
ENV_OUTPUT_DIR = "OUTPUT_DIR"
ENV_CONFIG_PATH = "OPERATOR_CONFIG"
CONFIG_RELATIVE = ("configs", "config.yaml")

STATUS_SUCCESS = "success"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

BADLINE_REASON_KEY = {
    tokens.REASON_JSON_PARSE: "badline_json_parse_error",
    tokens.REASON_NOT_OBJECT: "badline_record_not_object",
    tokens.REASON_ENCODING: "badline_encoding_error",
}

ORIGIN_REASON_KEY = {
    tokens.REASON_MISSING_IDENTIFIER: "origin_missing_identifier",
    tokens.REASON_UNSAFE_MEDIA_PATH: "origin_unsafe_media_path",
}

PENDING_REASON_KEY = {
    tokens.REASON_NO_QUALITY_CONCLUSION: "pending_no_quality_conclusion",
    tokens.REASON_ROUTE_DISABLED: "pending_route_disabled",
    tokens.REASON_NO_APPLICABLE_ROUTE: "pending_no_applicable_route",
    tokens.REASON_MISSING_TEXT: "pending_missing_text",
    tokens.REASON_LICENSE_MISSING: "pending_license_missing",
    tokens.REASON_MARKER_VALIDATION_FAILED: "pending_marker_validation_failed",
}

UNTRAINABLE_REASON_KEY = {
    tokens.REASON_LICENSE_NOT_TRAINABLE: "untrainable_license_not_trainable",
}

ISOLATED_REASON_KEY = {
    tokens.REASON_UPSTREAM_ISOLATED: "isolated_upstream",
}

MAX_RUN_DIRS = 999


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
        for key, value in allowed_overrides.items():
            if key == "routes" and isinstance(value, dict) \
                    and isinstance(raw.get("routes"), dict):
                merged = dict(raw["routes"])
                merged.update(value)
                raw["routes"] = merged
            else:
                raw[key] = value
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


# [v4.2] 血缘输出已停用：不再收集先前运行的阶段上下文与血缘摘要（utils/lineage.py 保留存档）。
# def _collect_prior_lineage(task_dir: Path,
#                            current_operator_dir: Path) -> Tuple[List[Dict[str, Any]],
#                                                                 List[Dict[str, Any]]]:
#     """收集同一任务内先前运行的阶段上下文与血缘摘要（跨算子衔接依据）。"""
#     contexts: List[Dict[str, Any]] = []
#     summaries: List[Dict[str, Any]] = []
#     if not task_dir.is_dir():
#         return contexts, summaries
#     for operator_path in sorted(task_dir.iterdir()):
#         if not operator_path.is_dir() or operator_path == current_operator_dir:
#             continue
#         for run_path in sorted(operator_path.iterdir()):
#             if not run_path.is_dir():
#                 continue
#             context_file = run_path / lineage.STAGE_CONTEXT_NAME
#             if context_file.is_file():
#                 try:
#                     with open(context_file, "r", encoding="utf-8") as handle:
#                         payload = json.load(handle)
#                 except (json.JSONDecodeError, UnicodeDecodeError, OSError):
#                     continue
#                 if isinstance(payload, dict):
#                     contexts.append(payload)
#             for node in lineage.read_lineage(run_path / lineage.LINEAGE_NAME):
#                 summaries.append({"operator": node.get("operator"),
#                                   "operator_version": node.get("operator_version"),
#                                   "status": node.get("status"),
#                                   "stageId": node.get("stageId"),
#                                   "runDir": node.get("runDir"),
#                                   "finishedAt": node.get("finishedAt")})
#     return contexts, summaries


# --------------------------------------------------------------------------- #
# 媒体链接存在性（**只探测，不搬运**）
# --------------------------------------------------------------------------- #

def _link_status(spec: Dict[str, Any], roots: Sequence[Path]) -> Dict[str, bool]:
    """探测被引用媒体在本地是否可见（对象存储链接策略下仅用于登记，不搬运文件）。"""
    status: Dict[str, bool] = {}
    for item in spec.get("media_links") or []:
        path = item["path"]
        if path in status:
            continue
        found = False
        for root in roots:
            try:
                candidate = io_utils.resolve_media(root, path)
            except io_utils.UnsupportedMediaPath:
                continue
            if candidate.is_file():
                found = True
                break
        status[path] = found
    return status


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(*, input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True) -> Dict[str, Any]:
    """执行一次完整的 Token 生成。返回结果字典（供 CLI 与 HTTP 层复用）。"""
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
        with RunLogger(run_dir / names.LOG_NAME) as logger:
            result = _process(input_path=input_path, task_dir=task_dir,
                              run_dir=run_dir, run_id=run_id, task_id=task_id,
                              config=config, started_at=started_at, logger=logger)
    return result


def _process(*, input_path: Path, task_dir: Path, run_dir: Path, run_id: str,
             task_id: str, config: OperatorConfig, started_at: str,
             logger: RunLogger) -> Dict[str, Any]:
    samples_path = run_dir / names.SAMPLES_FILE_NAME
    tokens_path = run_dir / names.TOKENS_NAME
    metadata_path = run_dir / names.METADATA_INDEX_NAME
    pending_path = run_dir / names.PENDING_NAME
    flagged_path = run_dir / names.FLAGGED_NAME
    untrainable_path = run_dir / names.UNTRAINABLE_NAME
    isolated_path = run_dir / names.ISOLATED_NAME
    rejects_path = run_dir / names.REJECTS_NAME
    # [v4.2] 血缘输出已停用：不再生成本地血缘/阶段上下文文件
    # lineage_path = run_dir / lineage.LINEAGE_NAME
    # context_path = run_dir / lineage.STAGE_CONTEXT_NAME

    route_map = tokens.route_files(config)
    roots = (input_path.parent, task_dir)

    stats: Dict[str, Any] = {
        "input_records": 0, "valid_records": 0, "blank_lines": 0, "bad_lines": 0,
        "generated_records": 0, "pending_records": 0, "untrainable_records": 0,
        "isolated_skipped_records": 0, "origin_quarantined": 0, "rejects_entries": 0,
        "media_links": 0, "media_links_unresolved": 0,
        "tokens_total": 0, "metadata_index_records": 0, "multi_route_records": 0,
        "flagged_records": 0,
        "marker_validation_failures": 0, "marker_reference_missing": 0,
        "local_annotation_records": 0,
        "empty_input": False, "encoding_error": False, "file_read_incomplete": False,
        "conservation_ok": True, "conservation_failures": [],
        "model": None, "models_available": False, "rule_based": True,
        "score_fields_produced": False, "subword_tokenizer": None,
        "degraded": False, "compressed": False, "version_state_changed": False,
        "media_storage_policy": "original_files_link_only",
        "enabled": bool(config.enabled),
        "routes_enabled": list(config.enabled_routes),
        "samples_file": names.SAMPLES_FILE_NAME,
        "missing_quality_policy": config.missing_quality_policy,
    }
    pending_by_reason: Dict[str, int] = {}
    untrainable_by_reason: Dict[str, int] = {}
    isolated_by_reason: Dict[str, int] = {}
    origin_by_reason: Dict[str, int] = {}
    bad_line_by_reason: Dict[str, int] = {}
    by_route: Dict[str, int] = {route: 0 for route in route_map}
    primary_by_route: Dict[str, int] = {}
    seen_identifiers: set = set()

    logger.event("run_start", "开始生成跨模态Token", label="输入",
                 inputFile=input_path.name, uniqueId=task_id, runId=run_id,
                 routes=",".join(config.enabled_routes),
                 samplesFile=names.SAMPLES_FILE_NAME,
                 excludeIsolated=config.exclude_isolated)

    encoding_error = False
    route_writers = {route: io_utils.JsonlWriter(run_dir / filename)
                     for route, filename in route_map.items()}
    try:
        with io_utils.JsonlWriter(samples_path) as samples_writer, \
                io_utils.JsonlWriter(tokens_path) as tokens_writer, \
                io_utils.JsonlWriter(metadata_path) as metadata_writer, \
                io_utils.JsonlWriter(pending_path) as pending_writer, \
                io_utils.JsonlWriter(flagged_path) as flagged_writer, \
                io_utils.JsonlWriter(untrainable_path) as untrainable_writer, \
                io_utils.JsonlWriter(isolated_path) as isolated_writer, \
                io_utils.JsonlWriter(rejects_path) as rejects_writer:
            line_iter = io_utils.iter_decoded_lines(input_path)
            line_no = 0
            while True:
                try:
                    raw_line = next(line_iter)
                except StopIteration:
                    break
                except UnicodeDecodeError as exc:
                    # 该行确实存在，只是无法按 UTF-8 解码：计入输入但不算可用记录
                    encoding_error = True
                    stats["encoding_error"] = True
                    stats["file_read_incomplete"] = True
                    stats["input_records"] += 1
                    stats["bad_lines"] += 1
                    stats["rejects_entries"] += 1
                    _bump(bad_line_by_reason, tokens.REASON_ENCODING)
                    rejects_writer.write(tokens.make_reject_entry(
                        line=line_no + 1, reason=tokens.REASON_ENCODING,
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
                    _bump(bad_line_by_reason, tokens.REASON_JSON_PARSE)
                    rejects_writer.write(tokens.make_reject_entry(
                        line=line_no, reason=tokens.REASON_JSON_PARSE,
                        detail="JSON 解析失败，进入原始隔离区", detected_at=now,
                        raw_text=text))
                    continue

                if not isinstance(payload, dict):
                    stats["bad_lines"] += 1
                    stats["rejects_entries"] += 1
                    _bump(bad_line_by_reason, tokens.REASON_NOT_OBJECT)
                    rejects_writer.write(tokens.make_reject_entry(
                        line=line_no, reason=tokens.REASON_NOT_OBJECT,
                        detail="记录不是 JSON 对象，进入原始隔离区", detected_at=now,
                        raw_text=text))
                    continue

                stats["valid_records"] += 1
                spec = upstream.resolve_record(payload)
                identifier = spec["identifier"]
                if identifier and identifier in seen_identifiers:
                    # 表1 #22 未要求去重；重复标识只计数并单独登记，不与主输出混算
                    stats["origin_quarantined"] += 1
                    stats["rejects_entries"] += 1
                    _bump(origin_by_reason, "duplicate_identifier")
                    rejects_writer.write(tokens.make_reject_entry(
                        line=line_no, reason="duplicate_identifier",
                        detail="标识与前面的记录重复（%s）" % identifier,
                        detected_at=now, identifier=identifier))
                    continue
                if identifier:
                    seen_identifiers.add(identifier)

                status = _link_status(spec, roots)
                stats["media_links"] += len(spec["media_links"])
                stats["media_links_unresolved"] += sum(
                    1 for value in status.values() if not value)

                outcome = tokens.evaluate_record(spec, config=config, event_time=now,
                                                 link_status=status)
                action = outcome["action"]
                if action == tokens.ACTION_GENERATE:
                    samples_writer.write(tokens.build_sample_record(
                        payload, outcome, route_files=route_map))
                    tokens_writer.write(tokens.build_token_row(
                        payload, outcome, event_time=now))
                    metadata_writer.write(tokens.build_metadata_row(
                        payload, outcome, event_time=now))
                    for route in outcome["routes"]:
                        writer = route_writers.get(route)
                        if writer is not None:
                            writer.write(tokens.build_sample_record(
                                payload, outcome, route_files=route_map))
                            by_route[route] = by_route.get(route, 0) + 1
                    stats["generated_records"] += 1
                    stats["tokens_total"] += 1
                    stats["metadata_index_records"] += 1
                    stats["multi_route_records"] += max(0, len(outcome["routes"]) - 1)
                    if names.ROUTE_LOCAL_ANNOTATION in outcome["routes"]:
                        stats["local_annotation_records"] += 1
                    primary = outcome["primary_route"]
                    primary_by_route[primary] = primary_by_route.get(primary, 0) + 1
                    if not outcome["marker_validation"].get("valid", False):
                        stats["marker_validation_failures"] += 1
                    if not outcome["marker_validation"].get("reference_ok", True):
                        stats["marker_reference_missing"] += 1
                    if (outcome.get("quality") or {}).get("flagged"):
                        # #20 的 flagged 属「不拦样本」旁路子集：已生成 Token，但仍单列复核
                        flagged_entry = tokens.make_ledger_entry(
                            line=line_no, spec=spec, outcome=outcome,
                            detail=outcome["detail"], detected_at=now)
                        flagged_entry["label_detail"] = "质量结论 flagged（旁路），已生成 Token 并列入复核"
                        flagged_writer.write(flagged_entry)
                        stats["flagged_records"] += 1
                elif action == tokens.ACTION_PENDING:
                    pending_writer.write(tokens.make_ledger_entry(
                        line=line_no, spec=spec, outcome=outcome,
                        detail=outcome["detail"], detected_at=now))
                    stats["pending_records"] += 1
                    _bump(pending_by_reason, outcome["reason"])
                elif action == tokens.ACTION_UNTRAINABLE:
                    untrainable_writer.write(tokens.make_ledger_entry(
                        line=line_no, spec=spec, outcome=outcome,
                        detail=outcome["detail"], detected_at=now))
                    stats["untrainable_records"] += 1
                    _bump(untrainable_by_reason, outcome["reason"])
                elif action == tokens.ACTION_ISOLATED:
                    isolated_writer.write(tokens.make_ledger_entry(
                        line=line_no, spec=spec, outcome=outcome,
                        detail=outcome["detail"], detected_at=now))
                    stats["isolated_skipped_records"] += 1
                    _bump(isolated_by_reason, outcome["reason"])
                else:
                    rejects_writer.write(tokens.make_reject_entry(
                        line=line_no, reason=outcome["reason"],
                        detail=outcome["detail"], detected_at=now,
                        identifier=identifier))
                    stats["origin_quarantined"] += 1
                    stats["rejects_entries"] += 1
                    _bump(origin_by_reason, outcome["reason"])
    finally:
        for writer in route_writers.values():
            writer.close()

    stats["empty_input"] = stats["input_records"] == 0
    stats["by_route"] = by_route
    stats["primary_by_route"] = primary_by_route
    stats["pending_by_reason"] = pending_by_reason
    stats["untrainable_by_reason"] = untrainable_by_reason
    stats["isolated_skipped_by_reason"] = isolated_by_reason
    stats["origin_by_reason"] = origin_by_reason
    stats["bad_line_by_reason"] = bad_line_by_reason
    for mapping, bucket in ((PENDING_REASON_KEY, pending_by_reason),
                            (UNTRAINABLE_REASON_KEY, untrainable_by_reason),
                            (ISOLATED_REASON_KEY, isolated_by_reason),
                            (ORIGIN_REASON_KEY, origin_by_reason),
                            (BADLINE_REASON_KEY, bad_line_by_reason)):
        for reason, key in mapping.items():
            stats[key] = bucket.get(reason, 0)
    stats["duplicate_identifier_records"] = origin_by_reason.get("duplicate_identifier", 0)

    stats["file_counts"] = _file_counts(run_dir, config, stats)
    stats["conservation_ok"], stats["conservation_failures"] = _check_conservation(stats)
    status = _resolve_status(stats, config)
    stats["degraded"] = status == STATUS_DEGRADED

    finished_at = lineage.now_iso()
    artifacts = _build_artifacts(run_dir, config)
    # [v4.2] 血缘输出已停用：血缘节点构造、血缘文件写入、阶段上下文写入全部停用
    # （utils/lineage.py 保留存档，不再调用）。
    # node = _build_lineage_node(task_id=task_id, run_id=run_id, run_dir=run_dir,
    #                           input_path=input_path, config=config, stats=stats,
    #                           status=status, started_at=started_at,
    #                           finished_at=finished_at, artifacts=artifacts,
    #                           output_file=samples_path)
    # lineage.write_lineage(lineage_path, [node])
    #
    # prior_contexts, prior_summaries = _collect_prior_lineage(task_dir,
    #                                                         task_dir / OPERATOR_DIR_NAME)
    # context = {"format": "crossmodal-stage-context-v1", "stage_id": config.stage_id,
    #            "uniqueId": task_id, "runId": run_id, "operator": names.OPERATOR,
    #            "operatorVersion": names.OPERATOR_VERSION,
    #            "formalOperatorNumber": names.OPERATOR_NUMBER,
    #            "entrypoint": names.SAMPLES_FILE_NAME,
    #            "inputFile": str(input_path), "runDir": str(run_dir),
    #            "status": status, "startedAt": started_at, "finishedAt": finished_at,
    #            "config": lineage.config_snapshot(config),
    #            "statistics": lineage.statistics_snapshot(stats),
    #            "artifacts": artifacts, "priorStageContexts": prior_contexts,
    #            "priorLineage": prior_summaries}
    # lineage.write_stage_context(context_path, context)

    manifest = transport.build_manifest(
        names.SAMPLES_FILE_NAME,
        [{"path": item["path"], "role": item["role"]} for item in artifacts]
        + [{"path": transport.MANIFEST_NAME, "role": "sidecar"}])
    transport.write_manifest(run_dir, manifest)

    logger.event("run_finish", "Token 生成完成", label="完成", status=status,
                 input=stats["input_records"], valid=stats["valid_records"],
                 generated=stats["generated_records"],
                 pending=stats["pending_records"],
                 untrainable=stats["untrainable_records"],
                 isolated=stats["isolated_skipped_records"],
                 origin=stats["origin_quarantined"],
                 conservationOk=stats["conservation_ok"])

    return {
        "status": status,
        "message": _status_message(status, stats),
        "operator": names.OPERATOR,
        "operatorVersion": names.OPERATOR_VERSION,
        "formalOperatorNumber": names.OPERATOR_NUMBER,
        "stageId": config.stage_id,
        "uniqueId": task_id,
        "runId": run_id,
        "taskDir": str(task_dir),
        "runDir": str(run_dir),
        "inputFile": str(input_path),
        "entrypoint": names.SAMPLES_FILE_NAME,
        "outputFile": str(samples_path),
        "tokensFile": str(tokens_path),
        "metadataIndexFile": str(metadata_path),
        "pendingFile": str(pending_path),
        "flaggedFile": str(flagged_path),
        "untrainableFile": str(untrainable_path),
        "isolatedFile": str(isolated_path),
        "rejectsFile": str(rejects_path),
        "routeFiles": {route: str(run_dir / filename)
                       for route, filename in route_map.items()},
        # [v4.2] 血缘输出已停用：run() 返回不再包含 lineageFile/contextFile/
        # stageLineage/priorLineage/priorStageContexts 等血缘键。
        "manifestPath": str(run_dir / transport.MANIFEST_NAME),
        "logFile": str(run_dir / names.LOG_NAME),
        "artifacts": artifacts,
        "statistics": stats,
        "model": None,
        "modelsAvailable": False,
        "ruleBased": True,
        "mediaStoragePolicy": "original_files_link_only",
        "compressed": False,
        "versionStateChanged": False,
        "transport": {"format": config.transport.format,
                      "maxResponseBytes": config.transport.max_response_bytes},
        "config": lineage.config_snapshot(config),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "entrypointNames": _entrypoint_names(config),
    }


def _bump(counter: Dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _entrypoint_names(config: OperatorConfig) -> List[str]:
    return [names.SAMPLES_FILE_NAME] + [names.ROUTE_FILES[route]
                                        for route in config.enabled_routes
                                        if names.ROUTE_FILES[route]
                                        != names.SAMPLES_FILE_NAME] \
        + [names.TOKENS_NAME, names.METADATA_INDEX_NAME, names.PENDING_NAME,
           names.FLAGGED_NAME, names.UNTRAINABLE_NAME, names.ISOLATED_NAME,
           names.REJECTS_NAME]


def _file_counts(run_dir: Path, config: OperatorConfig,
                 stats: Dict[str, Any]) -> Dict[str, int]:
    """读取产物真实行数（守恒校验以磁盘文件为准，不用内存计数自证）。"""
    counts = {"samples_records": io_utils.count_lines(run_dir / names.SAMPLES_FILE_NAME),
              "tokens_records": io_utils.count_lines(run_dir / names.TOKENS_NAME),
              "metadata_records": io_utils.count_lines(run_dir / names.METADATA_INDEX_NAME),
              "pending_records": io_utils.count_lines(run_dir / names.PENDING_NAME),
              "flagged_records": io_utils.count_lines(run_dir / names.FLAGGED_NAME),
              "untrainable_records": io_utils.count_lines(run_dir / names.UNTRAINABLE_NAME),
              "isolated_records": io_utils.count_lines(run_dir / names.ISOLATED_NAME),
              "rejects_records": io_utils.count_lines(run_dir / names.REJECTS_NAME)}
    for route in stats.get("by_route", {}):
        counts["route_%s_records" % route] = io_utils.count_lines(
            run_dir / names.ROUTE_FILES[route])
    return counts


def _resolve_status(stats: Dict[str, Any], config: OperatorConfig) -> str:
    if not stats.get("conservation_ok", True):
        return STATUS_FAILED
    if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
        return STATUS_FAILED
    if stats["marker_validation_failures"] > 0:
        return STATUS_FAILED
    if stats["file_counts"].get("samples_records", -1) < 0:
        return STATUS_FAILED
    if config.strict_quality and stats["pending_records"] > 0:
        return STATUS_FAILED
    if stats["generated_records"] == 0 and stats["pending_records"] > 0:
        return STATUS_DEGRADED
    return STATUS_SUCCESS


def _check_conservation(stats: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """核对「输入 = 各类去向」与「统计 = 落盘产物真实行数」两类守恒等式。"""
    counts = stats.get("file_counts") or {}
    by_route = stats.get("by_route") or {}
    route_file_total = sum(counts.get("route_%s_records" % route, 0)
                           for route in by_route)
    by_route_total = sum(by_route.values())
    checks = [
        ("input_records==valid_records+bad_lines",
         stats["input_records"], stats["valid_records"] + stats["bad_lines"]),
        ("valid_records==generated+pending+untrainable+isolated+origin",
         stats["valid_records"],
         stats["generated_records"] + stats["pending_records"]
         + stats["untrainable_records"] + stats["isolated_skipped_records"]
         + stats["origin_quarantined"]),
        ("generated==samples_file_records",
         stats["generated_records"], counts.get("samples_records", -1)),
        ("generated==tokens_records",
         stats["generated_records"], counts.get("tokens_records", -1)),
        ("generated==metadata_index_records",
         stats["generated_records"], counts.get("metadata_records", -1)),
        ("generated==sum(primary_by_route)",
         stats["generated_records"], sum((stats.get("primary_by_route") or {}).values())),
        ("by_route==sum(route_file_records)", by_route_total, route_file_total),
        ("sum(by_route)==generated+multi_route_records",
         by_route_total, stats["generated_records"] + stats["multi_route_records"]),
        ("pending_records==pending_file_records",
         stats["pending_records"], counts.get("pending_records", -1)),
        ("pending_records==sum(pending_by_reason)",
         stats["pending_records"], sum((stats.get("pending_by_reason") or {}).values())),
        ("untrainable_records==untrainable_file_records",
         stats["untrainable_records"], counts.get("untrainable_records", -1)),
        ("untrainable_records==sum(untrainable_by_reason)",
         stats["untrainable_records"],
         sum((stats.get("untrainable_by_reason") or {}).values())),
        ("flagged_records==flagged_file_records",
         stats["flagged_records"], counts.get("flagged_records", -1)),
        ("flagged_records<=generated_records",
         stats["flagged_records"] <= stats["generated_records"], True),
        ("isolated_skipped_records==isolated_file_records",
         stats["isolated_skipped_records"], counts.get("isolated_records", -1)),
        ("isolated_skipped_records==sum(isolated_by_reason)",
         stats["isolated_skipped_records"],
         sum((stats.get("isolated_skipped_by_reason") or {}).values())),
        ("origin_quarantined==sum(origin_by_reason)",
         stats["origin_quarantined"], sum((stats.get("origin_by_reason") or {}).values())),
        ("bad_lines==sum(bad_line_by_reason)",
         stats["bad_lines"], sum((stats.get("bad_line_by_reason") or {}).values())),
        ("rejects_entries==rejects_file_records",
         stats["rejects_entries"], counts.get("rejects_records", -1)),
        ("rejects_entries==bad_lines+origin_quarantined",
         stats["rejects_entries"], stats["bad_lines"] + stats["origin_quarantined"]),
        ("marker_validation_failures==0", 0, stats["marker_validation_failures"]),
    ]
    failures = ["%s（实际 %s != 期望 %s）" % (name, left, right)
                for name, left, right in checks if left != right]
    if stats["media_links_unresolved"] > stats["media_links"]:
        failures.append("media_links_unresolved<=media_links（%s > %s）"
                        % (stats["media_links_unresolved"], stats["media_links"]))
    if stats["marker_reference_missing"] > stats["generated_records"]:
        failures.append("marker_reference_missing<=generated（%s > %s）"
                        % (stats["marker_reference_missing"],
                           stats["generated_records"]))
    if stats["tokens_total"] != stats["generated_records"]:
        failures.append("tokens_total==generated（%s != %s）"
                        % (stats["tokens_total"], stats["generated_records"]))
    return (not failures), failures


def _build_artifacts(run_dir: Path, config: OperatorConfig) -> List[Dict[str, Any]]:
    names_roles: List[Tuple[str, str]] = [(names.SAMPLES_FILE_NAME, "records")]
    for route in config.enabled_routes:
        filename = names.ROUTE_FILES[route]
        if filename != names.SAMPLES_FILE_NAME:
            names_roles.append((filename, "records"))
    names_roles.extend([(names.TOKENS_NAME, "records"),
                        (names.METADATA_INDEX_NAME, "records"),
                        (names.PENDING_NAME, "records"),
                        (names.FLAGGED_NAME, "records"),
                        (names.UNTRAINABLE_NAME, "records"),
                        (names.ISOLATED_NAME, "records"),
                        (names.REJECTS_NAME, "records"),
                        (names.LOG_NAME, "log"),
                        (lineage.LINEAGE_NAME, "sidecar"),
                        (lineage.STAGE_CONTEXT_NAME, "sidecar")])
    entries: List[Dict[str, Any]] = []
    for name, role in names_roles:
        path = run_dir / name
        if path.is_file():
            entries.append({"path": name, "role": role, "size": path.stat().st_size})
    return entries


def _build_lineage_node(*, task_id: str, run_id: str, run_dir: Path, input_path: Path,
                        config: OperatorConfig, stats: Dict[str, Any], status: str,
                        started_at: str, finished_at: str,
                        artifacts: Sequence[Dict[str, Any]],
                        output_file: Path) -> Dict[str, Any]:
    return {
        "operator": names.OPERATOR,
        "operator_version": names.OPERATOR_VERSION,
        "formalOperatorNumber": names.OPERATOR_NUMBER,
        "stageId": config.stage_id,
        "uniqueId": task_id,
        "runId": run_id,
        "runDir": str(run_dir),
        "inputFile": str(input_path),
        "outputFile": str(output_file),
        "outputArtifact": names.SAMPLES_FILE_NAME,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": lineage.config_snapshot(config),
        "statistics": lineage.statistics_snapshot(stats),
        "artifacts": list(artifacts),
        "model": None,
        "modelsAvailable": False,
        "ruleBased": True,
        "mediaStoragePolicy": "original_files_link_only",
        "compressed": False,
        "versionStateChanged": False,
        "lineageFormat": "crossmodal-lineage-v1",
    }


def _strip_output_file(node: Dict[str, Any]) -> Dict[str, Any]:
    stripped = dict(node)
    stripped.pop("outputFile", None)
    return stripped


def _status_message(status: str, stats: Dict[str, Any]) -> str:
    if status == STATUS_FAILED:
        if stats["conservation_ok"] is False:
            return "统计守恒校验失败：" + "；".join(stats["conservation_failures"])
        if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
            return "全部 %d 行均无法解析，未产生任何可用样本" % stats["bad_lines"]
        if stats["marker_validation_failures"] > 0:
            return "模态标记校验失败 %d 条，按配置显式失败" % stats["marker_validation_failures"]
        return ("严格模式（missing_quality_policy=failed）下存在 %d 条无质量结论记录，"
                "按配置显式失败" % stats["pending_records"])
    if status == STATUS_DEGRADED:
        return ("%d 条记录尚无质量结论，未生成 Token，等待上游/人工补充"
                % stats["pending_records"])
    if stats["empty_input"]:
        return "输入为空，未生成任何 Token（empty_input=true，非异常）"
    return ("已生成 Token %d 条；样本文件 %d 条；待处理 %d 条；不可训练 %d 条；"
            "隔离跳过 %d 条；原始隔离 %d 条"
            % (stats["generated_records"],
               stats["file_counts"].get("samples_records", 0),
               stats["pending_records"], stats["untrainable_records"],
               stats["isolated_skipped_records"], stats["origin_quarantined"]))


# --------------------------------------------------------------------------- #
# 阶段血缘形态
# --------------------------------------------------------------------------- #
#
# [v4.2] 血缘输出已停用：lineageFile 返回形态不再使用（响应统一见 app.py §3 改造）。
#
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """阶段中间响应：只返回当前节点的 ``lineageFile`` 对象（含临时 outputFile）。"""
#     node = dict(result["stageLineage"][0])
#     node["outputFile"] = result["outputFile"]
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
        description="算子 22 生成跨模态Token（系统功能型，无模型）")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径")
    parser.add_argument("--unique-id", required=True, help="任务标识")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="输出根目录")
    parser.add_argument("--override", default=None, help="请求级配置覆盖（JSON 对象）")
    parser.add_argument("--restrict-to-task", action="store_true",
                        help="只允许输入位于任务目录内（默认允许任意本地路径）")
    # [v4.2] 血缘输出已停用：--print-lineage 选项注释保留、暂不删除
    # parser.add_argument("--print-lineage", action="store_true",
    #                     help="额外打印阶段中间血缘形态")
    return parser


def _ensure_utf8_stdout() -> None:
    """统一 CLI 输出编码为 UTF-8，避免中文在 Windows GBK 控制台被转义或报错。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="strict")
    except (ValueError, OSError):                            # pragma: no cover
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_utf8_stdout()
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
        payload = {"code": 1, "msg": str(exc), "data": {"errorType": type(exc).__name__}}
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    except io_utils.TaskBusyError as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {"errorType": "TaskBusy"}},
                         ensure_ascii=False))
        return 2

    data = result
    # [v4.2] 血缘输出已停用：--print-lineage 分支注释保留、暂不删除
    # if args.print_lineage:
    #     data = dict(result)
    #     data["intermediateLineage"] = build_intermediate_lineage(result)
    print(json.dumps({"code": 0, "msg": result["message"], "data": data},
                     ensure_ascii=False))
    return 1 if result["status"] == STATUS_FAILED else 0


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(main())
