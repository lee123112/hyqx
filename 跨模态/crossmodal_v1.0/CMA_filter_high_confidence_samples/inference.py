"""算子 19「筛选高置信样本入池」编排层：校验 → 流式判定 → 台账 → 血缘。

表1 #19：默认开启、**必须**、规则型、P0；无模型、无外部服务，因此**不存在**
「模型不可用但仍标 model」的伪造降级路径——所有降级都表现为
``waiting_manual``（待人工，流程状态）或 ``failed``（严格模式下显式失败）。

输出目录结构：``OUTPUT_DIR/<uniqueId>/high_confidence_pool/<runId>/``
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

def _stage_output_name():
    """本阶段输出目录名：<NN>_<算子目录名>（编号读包根 map.json）。"""
    import json as _json
    here = Path(__file__).resolve().parent
    try:
        operators = _json.loads((here.parent / "map.json").read_text(encoding="utf-8"))["operators"]
        for item in operators.values():
            if item.get("package") == here.name:
                return "%02d_%s" % (int(item["formalOperatorNumber"]), here.name)
    except Exception:
        pass
    return here.name


def publish_stage_output(run_dir):
    """[2026-10-24] 设置 CM_RESULTS_ROOT 时，把本阶段最终产物直接发布到
    <root>/<NN>_<算子目录>/：JSONL/JSON（入口、台账、统计）+ 内容有变化的媒体；
    与输入完全相同（按内容哈希判定）的透传媒体不复制。未设置时行为不变。

    输入根识别兼容两种任务目录布局：`<task>/input/`（同机暂存）与
    `<task>/` 根目录直接铺开（/uploadFile 解压，入口 JSONL 在任务根）。
    """
    import hashlib
    import os
    import shutil
    base = os.environ.get("CM_RESULTS_ROOT")
    if not base:
        return None
    run_dir = Path(run_dir)
    roots = []
    cursor = run_dir
    for _ in range(6):
        if (cursor / "input").is_dir():
            roots = [cursor / "input"]
            break
        if cursor != run_dir and any(cursor.glob("*.jsonl")):
            roots = [cursor]
            break
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    dest = Path(base) / _stage_output_name()
    tmp = dest.parent / (dest.name + ".publishing")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    def _iter_files(root):
        for candidate in root.rglob("*"):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate == run_dir or run_dir in candidate.parents:
                continue
            yield candidate

    skip_names = {"manifest.json", "config.snapshot.json", "run.log",
                  "lineage.json", "stage_lineage.json", "stage_context.json"}
    skip_suffixes = {".tmp", ".part", ".partial", ".lock"}
    candidates = []
    for source in sorted(run_dir.rglob("*")):
        try:
            if not source.is_file():
                continue
        except OSError:
            continue
        if source.name in skip_names or source.suffix.lower() in skip_suffixes:
            continue
        candidates.append(source)

    sizes = set()
    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                sizes.add(source.stat().st_size)
            except OSError:
                pass
    input_digests = {}
    for root in roots:
        for candidate in _iter_files(root):
            try:
                if candidate.stat().st_size not in sizes:
                    continue
                input_digests.setdefault(candidate.stat().st_size, set()).add(
                    hashlib.sha256(candidate.read_bytes()).digest())
            except OSError:
                continue

    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                digests = input_digests.get(source.stat().st_size)
                if digests and hashlib.sha256(source.read_bytes()).digest() in digests:
                    continue
            except OSError:
                pass
        target = tmp / source.relative_to(run_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest

from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from pydantic import ValidationError

from utils import io as io_utils
# [v4.2] 血缘输出已停用：lineage 仅保留 now_iso（通用时间工具）使用，血缘构造/写入调用已注释
from utils import lineage
from utils import pool_filter, transport
from utils.logger import RunLogger
from utils.schemas import ConfigError, OperatorConfig, validate_overrides

__all__ = [
    "InputError",
    "OPERATOR_DIR_NAME",
    "default_config_path",
    "default_output_dir",
    "load_config",
    "main",
    "run",
]

OPERATOR_DIR_NAME = "high_confidence_pool"
ENV_OUTPUT_DIR = "OUTPUT_DIR"
ENV_CONFIG_PATH = "OPERATOR_CONFIG"
CONFIG_RELATIVE = ("configs", "config.yaml")

STATUS_SUCCESS = "success"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

BADLINE_REASON_KEY = {
    pool_filter.REASON_JSON_PARSE: "badline_json_parse_error",
    pool_filter.REASON_NOT_OBJECT: "badline_record_not_object",
    pool_filter.REASON_ENCODING: "badline_encoding_error",
}

ORIGIN_REASON_KEY = {
    pool_filter.REASON_MISSING_IDENTIFIER: "origin_missing_identifier",
    pool_filter.REASON_DUPLICATE_IDENTIFIER: "origin_duplicate_identifier",
    pool_filter.REASON_UNSAFE_MEDIA_PATH: "origin_unsafe_media_path",
}

ISOLATE_REASON_KEY = {
    pool_filter.REASON_ROUTE_FAILED: "isolated_route_failed",
    pool_filter.REASON_WHOLE_NOT_ALIGNED: "isolated_whole_not_aligned",
    pool_filter.REASON_WHOLE_NOT_REQUIRED_TYPE: "isolated_whole_not_required_type",
    pool_filter.REASON_LICENSE_NOT_TRAINABLE: "isolated_license_not_trainable",
    pool_filter.REASON_TEMPORAL_FAILED: "isolated_temporal_failed",
    pool_filter.REASON_UPSTREAM_ISOLATED: "isolated_upstream_isolated",
}

PENDING_REASON_KEY = {
    pool_filter.REASON_NO_ROUTE: "pending_no_scoring_route",
    pool_filter.REASON_INSUFFICIENT_ROUTES: "pending_insufficient_enabled_routes",
    pool_filter.REASON_ROUTE_PENDING: "pending_route_pending",
    pool_filter.REASON_WHOLE_MISSING: "pending_whole_alignment_missing",
    pool_filter.REASON_WHOLE_PENDING: "pending_whole_pending_manual_review",
    pool_filter.REASON_WHOLE_UNCERTAIN: "pending_whole_uncertain",
    pool_filter.REASON_LICENSE_MISSING: "pending_license_missing",
    pool_filter.REASON_TEMPORAL_MISSING: "pending_temporal_missing",
    pool_filter.REASON_TEMPORAL_PENDING: "pending_temporal_pending",
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


# [v4.2] 血缘输出已停用：_collect_prior_lineage 仅存档，已无调用点。
# def _collect_prior_lineage(task_dir: Path, current_run: Path) -> Tuple[List[Dict[str, Any]],
#                                                                       List[Dict[str, Any]]]:
#     """收集同一任务内先前运行的阶段上下文与血缘摘要（跨算子衔接依据）。"""
#     contexts: List[Dict[str, Any]] = []
#     summaries: List[Dict[str, Any]] = []
#     if not task_dir.is_dir():
#         return contexts, summaries
#     for operator_path in sorted(task_dir.iterdir()):
#         if not operator_path.is_dir() or operator_path == current_run:
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
# 媒体尽力搬运
# --------------------------------------------------------------------------- #

def _carry_media(run_dir: Path, refs: Sequence[str], roots: Sequence[Path]) -> List[Dict[str, Any]]:
    """把能找到的媒体文件复制到运行目录（去重、**完整保留声明的相对路径**）。

    缺失不报错（只由 ``media_missing`` 统计体现），不伪造文件。
    """
    carried: List[Dict[str, Any]] = []
    seen = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        for root in roots:
            try:
                source = io_utils.resolve_media(root, ref)
            except io_utils.UnsupportedMediaPath:
                continue
            if source.is_file():
                target = run_dir / ref
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                suffix = os.path.splitext(ref)[1].lower()
                carried.append({"path": ref,
                                "role": "media" if suffix in transport.MEDIA_SUFFIXES
                                else "other",
                                "size": target.stat().st_size})
                break
    return carried


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(*, input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True) -> Dict[str, Any]:
    """执行一次完整判定。返回结果字典（供 CLI 与 HTTP 层复用）。"""
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
        with RunLogger(run_dir / pool_filter.LOG_NAME) as logger:
            result = _process(input_path=input_path, task_dir=task_dir,
                              run_dir=run_dir, run_id=run_id, task_id=task_id,
                              config=config, started_at=started_at, logger=logger)
    return result


def _process(*, input_path: Path, task_dir: Path, run_dir: Path, run_id: str,
             task_id: str, config: OperatorConfig, started_at: str,
             logger: RunLogger) -> Dict[str, Any]:
    main_path = run_dir / pool_filter.MAIN_NAME
    quarantine_path = run_dir / pool_filter.QUARANTINE_NAME
    pending_path = run_dir / pool_filter.PENDING_NAME
    rejects_path = run_dir / pool_filter.REJECTS_NAME
    # [v4.2] 血缘/阶段归档已停用：lineage.json / stage_context.json 不再写入

    stats: Dict[str, Any] = {
        "input_records": 0, "valid_records": 0, "blank_lines": 0, "bad_lines": 0,
        "admitted_records": 0, "sample_isolated": 0, "pending_records": 0,
        "origin_quarantined": 0, "rejects_entries": 0,
        "media_refs": 0, "media_missing": 0, "media_carried": 0,
        "empty_input": False, "encoding_error": False, "file_read_incomplete": False,
        "conservation_ok": True, "conservation_failures": [],
        "model": None, "models_available": False, "rule_based": True,
        "local_annotation_hard_requirement": False, "score_fields_produced": False,
        "degraded": False, "enabled": bool(config.enabled),
        "required_alignment": config.required_alignment,
        "require_temporal_if_present": bool(config.require_temporal_if_present),
        "temporal_route_fallback": bool(config.temporal_route_fallback),
        "admitted_via_temporal_route": 0,
        "training_authorization": config.training_authorization,
        "min_enabled_routes": int(config.min_enabled_routes),
        "missing_upstream_policy": config.missing_upstream_policy,
    }
    isolated_by_reason: Dict[str, int] = {}
    pending_by_reason: Dict[str, int] = {}
    origin_by_reason: Dict[str, int] = {}
    bad_line_by_reason: Dict[str, int] = {}
    gate_counts = {name: {"passed": 0, "failed": 0, "pending": 0}
                   for name in pool_filter.GATE_ORDER}
    seen_identifiers: set = set()
    media_refs_ordered: List[str] = []

    logger.event("run_start", "开始筛选高置信样本入池",
                 inputFile=input_path.name, uniqueId=task_id, runId=run_id,
                 enabled=config.enabled, requiredAlignment=config.required_alignment)

    encoding_error = False
    with io_utils.JsonlWriter(main_path) as main_writer, \
            io_utils.JsonlWriter(quarantine_path) as quarantine_writer, \
            io_utils.JsonlWriter(pending_path) as pending_writer, \
            io_utils.JsonlWriter(rejects_path) as rejects_writer:
        line_iter = io_utils.iter_decoded_lines(input_path)
        line_no = 0
        while True:
            try:
                raw_line = next(line_iter)
            except StopIteration:
                break
            except UnicodeDecodeError as exc:
                # 该行确实存在，只是无法按 UTF-8 解码：计入输入但不计为可用记录
                encoding_error = True
                stats["encoding_error"] = True
                stats["input_records"] += 1
                stats["bad_lines"] += 1
                stats["rejects_entries"] += 1
                _bump(bad_line_by_reason, pool_filter.REASON_ENCODING)
                rejects_writer.write(pool_filter.make_reject_entry(
                    line=line_no + 1, reason=pool_filter.REASON_ENCODING,
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
                _bump(bad_line_by_reason, pool_filter.REASON_JSON_PARSE)
                rejects_writer.write(pool_filter.make_reject_entry(
                    line=line_no, reason=pool_filter.REASON_JSON_PARSE,
                    detail="JSON 解析失败，进入原始隔离区", detected_at=now,
                    raw_text=text))
                continue

            if not isinstance(payload, dict):
                stats["bad_lines"] += 1
                stats["rejects_entries"] += 1
                _bump(bad_line_by_reason, pool_filter.REASON_NOT_OBJECT)
                rejects_writer.write(pool_filter.make_reject_entry(
                    line=line_no, reason=pool_filter.REASON_NOT_OBJECT,
                    detail="记录不是 JSON 对象，进入原始隔离区", detected_at=now,
                    raw_text=text))
                continue

            stats["valid_records"] += 1
            spec = pool_filter.resolve_record(payload)
            identifier = spec["identifier"]
            media_refs_ordered.extend(spec["media_refs"])
            stats["media_refs"] += len(spec["media_refs"])

            if identifier and identifier in seen_identifiers:
                stats["origin_quarantined"] += 1
                stats["rejects_entries"] += 1
                _bump(origin_by_reason, pool_filter.REASON_DUPLICATE_IDENTIFIER)
                rejects_writer.write(pool_filter.make_reject_entry(
                    line=line_no, reason=pool_filter.REASON_DUPLICATE_IDENTIFIER,
                    detail="标识与前面的记录重复（%s）" % identifier,
                    detected_at=now, identifier=identifier))
                continue
            if identifier:
                seen_identifiers.add(identifier)

            missing = _count_missing_media(spec["media_refs"], (input_path.parent, task_dir))
            stats["media_missing"] += missing
            outcome = pool_filter.evaluate_record(
                spec, config=config, event_time=now, media_missing=missing)

            for name in pool_filter.GATE_ORDER:
                gate = outcome["gates"].get(name)
                if not gate:
                    continue
                if gate["passed"] is True:
                    gate_counts[name]["passed"] += 1
                elif gate["passed"] is False:
                    gate_counts[name]["failed"] += 1
                else:
                    gate_counts[name]["pending"] += 1

            if outcome["action"] == pool_filter.ACTION_ADMIT:
                main_writer.write(outcome["output"])
                stats["admitted_records"] += 1
                if outcome["gates"]["route"]["enabled_routes"] == ["15"]:
                    stats["admitted_via_temporal_route"] += 1
            elif outcome["action"] == pool_filter.ACTION_ISOLATE:
                quarantine_writer.write(pool_filter.make_ledger_entry(
                    line=line_no, spec=spec, outcome=outcome,
                    detail=outcome["detail"], detected_at=now))
                stats["sample_isolated"] += 1
                _bump(isolated_by_reason, outcome["reason"])
            elif outcome["action"] == pool_filter.ACTION_PENDING:
                pending_writer.write(pool_filter.make_ledger_entry(
                    line=line_no, spec=spec, outcome=outcome,
                    detail=outcome["detail"], detected_at=now))
                stats["pending_records"] += 1
                _bump(pending_by_reason, outcome["reason"])
            else:
                rejects_writer.write(pool_filter.make_ledger_entry(
                    line=line_no, spec=spec, outcome=outcome,
                    detail=outcome["detail"], detected_at=now))
                stats["origin_quarantined"] += 1
                stats["rejects_entries"] += 1
                _bump(origin_by_reason, outcome["reason"])

    media_carried = _carry_media(run_dir, media_refs_ordered,
                                 (input_path.parent, task_dir))
    stats["media_carried"] = len(media_carried)

    stats["empty_input"] = stats["input_records"] == 0
    stats["isolated_by_reason"] = isolated_by_reason
    stats["pending_by_reason"] = pending_by_reason
    stats["origin_by_reason"] = origin_by_reason
    stats["bad_line_by_reason"] = bad_line_by_reason
    stats["gate_counts"] = gate_counts
    for mapping, bucket in ((ISOLATE_REASON_KEY, isolated_by_reason),
                            (PENDING_REASON_KEY, pending_by_reason),
                            (ORIGIN_REASON_KEY, origin_by_reason),
                            (BADLINE_REASON_KEY, bad_line_by_reason)):
        for reason, key in mapping.items():
            stats[key] = bucket.get(reason, 0)

    stats["conservation_ok"], stats["conservation_failures"] = _check_conservation(stats)
    status = _resolve_status(stats, config)
    stats["degraded"] = status == STATUS_DEGRADED

    finished_at = lineage.now_iso()
    artifacts = _build_artifacts(run_dir, media_carried)
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘节点、不再写 lineage.json /
    # stage_context.json；相关键也不再随 result 返回。
    # node = _build_lineage_node(task_id=task_id, run_id=run_id, run_dir=run_dir,
    #                            input_path=input_path, config=config,
    #                            stats=stats, status=status, started_at=started_at,
    #                            finished_at=finished_at, artifacts=artifacts,
    #                            output_file=main_path)
    # lineage.write_lineage(lineage_path, [node])
    #
    # prior_contexts, prior_summaries = _collect_prior_lineage(task_dir,
    #                                                          task_dir / OPERATOR_DIR_NAME)
    # context = {"format": "crossmodal-stage-context-v1", "stage_id": config.stage_id,
    #            "uniqueId": task_id, "runId": run_id, "operator": pool_filter.OPERATOR,
    #            "operatorVersion": pool_filter.OPERATOR_VERSION,
    #            "formalOperatorNumber": pool_filter.OPERATOR_NUMBER,
    #            "entrypoint": pool_filter.MAIN_NAME,
    #            "inputFile": str(input_path), "runDir": str(run_dir),
    #            "status": status, "startedAt": started_at, "finishedAt": finished_at,
    #            "config": lineage.config_snapshot(config),
    #            "statistics": lineage.statistics_snapshot(stats),
    #            "artifacts": artifacts, "priorStageContexts": prior_contexts,
    #            "priorLineage": prior_summaries}
    # lineage.write_stage_context(context_path, context)

    manifest = transport.build_manifest(
        pool_filter.MAIN_NAME,
        [{"path": item["path"], "role": item["role"]} for item in artifacts]
        + [{"path": transport.MANIFEST_NAME, "role": "sidecar"}])
    transport.write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    logger.event("run_finish", "筛选完成", status=status,
                 input=stats["input_records"], valid=stats["valid_records"],
                 admitted=stats["admitted_records"],
                 isolated=stats["sample_isolated"], pending=stats["pending_records"],
                 origin=stats["origin_quarantined"],
                 conservationOk=stats["conservation_ok"])

    result = {
        "status": status,
        "message": _status_message(status, stats),
        "operator": pool_filter.OPERATOR,
        "operatorVersion": pool_filter.OPERATOR_VERSION,
        "formalOperatorNumber": pool_filter.OPERATOR_NUMBER,
        "stageId": config.stage_id,
        "uniqueId": task_id,
        "runId": run_id,
        "taskDir": str(task_dir),
        "runDir": str(run_dir),
        "inputFile": str(input_path),
        "entrypoint": pool_filter.MAIN_NAME,
        "outputFile": str(main_path),
        "quarantineFile": str(quarantine_path),
        "pendingFile": str(pending_path),
        "rejectsFile": str(rejects_path),
        # [v4.2] 血缘输出已停用：不再回传 lineageFile/contextFile/config/
        # stageLineage/priorLineage/priorStageContexts
        "manifestPath": str(run_dir / transport.MANIFEST_NAME),
        "logFile": str(run_dir / pool_filter.LOG_NAME),
        "artifacts": artifacts,
        "mediaArtifacts": media_carried,
        "statistics": stats,
        "model": None,
        "modelsAvailable": False,
        "ruleBased": True,
        "transport": {"format": config.transport.format,
                      "maxResponseBytes": config.transport.max_response_bytes},
        "startedAt": started_at,
        "finishedAt": finished_at,
        "entrypointNames": [pool_filter.MAIN_NAME, pool_filter.QUARANTINE_NAME,
                            pool_filter.PENDING_NAME, pool_filter.REJECTS_NAME],
    }
    return result


def _bump(counter: Dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _count_missing_media(refs: Sequence[str], roots: Sequence[Path]) -> int:
    missing = 0
    for ref in refs:
        found = False
        for root in roots:
            try:
                candidate = io_utils.resolve_media(root, ref)
            except io_utils.UnsupportedMediaPath:
                continue
            if candidate.is_file():
                found = True
                break
        if not found:
            missing += 1
    return missing


def _resolve_status(stats: Dict[str, Any], config: OperatorConfig) -> str:
    if not stats.get("conservation_ok", True):
        return STATUS_FAILED
    if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
        return STATUS_FAILED
    if config.strict_upstream and stats["pending_records"] > 0:
        return STATUS_FAILED
    if stats["admitted_records"] == 0 and stats["pending_records"] > 0:
        return STATUS_DEGRADED
    return STATUS_SUCCESS


def _check_conservation(stats: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """核对「输入 = 各类去向」以及「台账条目 = 对应分类数」两类守恒等式。"""
    equality_checks = [
        ("input_records==valid_records+bad_lines",
         stats["input_records"], stats["valid_records"] + stats["bad_lines"]),
        ("valid_records==admitted+isolated+pending+origin",
         stats["valid_records"],
         stats["admitted_records"] + stats["sample_isolated"]
         + stats["pending_records"] + stats["origin_quarantined"]),
        ("sample_isolated==sum(isolated_by_reason)",
         stats["sample_isolated"], sum(stats["isolated_by_reason"].values())),
        ("pending_records==sum(pending_by_reason)",
         stats["pending_records"], sum(stats["pending_by_reason"].values())),
        ("origin_quarantined==sum(origin_by_reason)",
         stats["origin_quarantined"], sum(stats["origin_by_reason"].values())),
        ("bad_lines==sum(bad_line_by_reason)",
         stats["bad_lines"], sum(stats["bad_line_by_reason"].values())),
        ("rejects_entries==bad_lines+origin_quarantined",
         stats["rejects_entries"], stats["bad_lines"] + stats["origin_quarantined"]),
    ]
    failures = ["%s（实际 %s != 期望 %s）" % (name, left, right)
                for name, left, right in equality_checks if left != right]
    if stats["media_missing"] > stats["media_refs"]:
        failures.append("media_missing<=media_refs（%s > %s）"
                        % (stats["media_missing"], stats["media_refs"]))
    if stats["admitted_records"] < 0 or stats["sample_isolated"] < 0:
        failures.append("出现负数统计，说明计数逻辑异常")
    return (not failures), failures


def _build_artifacts(run_dir: Path, media_carried: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for name, role in ((pool_filter.MAIN_NAME, "records"),
                       (pool_filter.QUARANTINE_NAME, "records"),
                       (pool_filter.PENDING_NAME, "records"),
                       (pool_filter.REJECTS_NAME, "records"),
                       (pool_filter.LOG_NAME, "log")):
        # [v4.2] lineage.json / stage_context.json 不再生成，故不再登记为产物
        path = run_dir / name
        if path.is_file():
            entries.append({"path": name, "role": role, "size": path.stat().st_size})
    for item in media_carried:
        entries.append({"path": item["path"], "role": item["role"],
                        "size": item.get("size")})
    return entries


# [v4.2] 血缘输出已停用：_build_lineage_node 仅存档，已无调用点。
# def _build_lineage_node(*, task_id: str, run_id: str, run_dir: Path, input_path: Path,
#                         config: OperatorConfig, stats: Dict[str, Any], status: str,
#                         started_at: str, finished_at: str,
#                         artifacts: Sequence[Dict[str, Any]],
#                         output_file: Path) -> Dict[str, Any]:
#     return {
#         "operator": pool_filter.OPERATOR,
#         "operator_version": pool_filter.OPERATOR_VERSION,
#         "formalOperatorNumber": pool_filter.OPERATOR_NUMBER,
#         "stageId": config.stage_id,
#         "uniqueId": task_id,
#         "runId": run_id,
#         "runDir": str(run_dir),
#         "inputFile": str(input_path),
#         "outputFile": str(output_file),
#         "outputArtifact": pool_filter.MAIN_NAME,
#         "status": status,
#         "startedAt": started_at,
#         "finishedAt": finished_at,
#         "config": lineage.config_snapshot(config),
#         "statistics": lineage.statistics_snapshot(stats),
#         "artifacts": list(artifacts),
#         "model": None,
#         "modelsAvailable": False,
#         "ruleBased": True,
#         "lineageFormat": "crossmodal-lineage-v1",
#     }


# [v4.2] 血缘输出已停用：_strip_output_file 仅存档，已无调用点。
# def _strip_output_file(node: Dict[str, Any]) -> Dict[str, Any]:
#     stripped = dict(node)
#     stripped.pop("outputFile", None)
#     return stripped


def _status_message(status: str, stats: Dict[str, Any]) -> str:
    if status == STATUS_FAILED:
        if stats["conservation_ok"] is False:
            return "统计守恒校验失败：" + "；".join(stats["conservation_failures"])
        if stats["valid_records"] == 0 and stats["bad_lines"] > 0:
            return "全部 %d 行均无法解析，未产生任何可用候选池记录" % stats["bad_lines"]
        return ("严格模式（missing_upstream_policy=failed）下存在 %d 条必过项结论缺失，"
                "按配置显式失败" % stats["pending_records"])
    if status == STATUS_DEGRADED:
        return ("所有 %d 条记录的四项必过项结论均未定，候选池为空，等待上游/人工补充"
                % stats["pending_records"])
    if stats["empty_input"]:
        return "输入为空，候选池为空（empty_input=true，非异常）"
    return ("候选池 %d 条；样本隔离 %d 条；待人工 %d 条；原始隔离 %d 条"
            % (stats["admitted_records"], stats["sample_isolated"],
               stats["pending_records"], stats["origin_quarantined"]))


def _count_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# 阶段血缘形态
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：以下两个响应构造函数仅服务旧 lineageFile 形态，保留注释存档。
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
        description="算子 19 筛选高置信样本入池（规则型，无模型）")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径")
    parser.add_argument("--unique-id", required=True, help="任务标识")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="输出根目录")
    parser.add_argument("--override", default=None, help="请求级配置覆盖（JSON 对象）")
    parser.add_argument("--restrict-to-task", action="store_true",
                        help="只允许输入位于任务目录内（默认允许任意本地路径）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
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

    payload = {"code": 0, "msg": result["message"], "data": result}
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if result["status"] == STATUS_FAILED else 0


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(main())
