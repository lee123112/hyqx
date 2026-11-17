"""算子 26「生成交付样例」推理入口（业务编排）。

职责：解析 #23 交付包（zip/目录/入口 JSONL）→ 逐条判定与计数 → 每条已开启路线
最多抽 10 条 → 导出 Excel 样例清单与样例报告 → 写血缘、清单与日志。

严格遵循跨模态共享契约：只写 ``OUTPUT_DIR/<uniqueId>/``；异常行进独立清单；
输出 UTF-8 且原子替换；统计全部来自真实结果；模型列 = ``--``（系统功能，规则判定）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import traceback
from pathlib import Path
from utils import delivery_handoff
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from utils import io as io_utils
# [v4.2] 血缘输出已停用：lineage 仅保留 now_iso/config_snapshot/statistics_snapshot/
# artifact_entry（通用工具）使用；read_lineage/write_lineage/write_stage_context
# 等血缘调用已全部注释存档。
from utils import lineage as lineage_utils
from utils import logger as logger_utils
from utils import sampling
from utils import schemas, transport, xlsx

OPERATOR_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = OPERATOR_DIR / "configs" / "config.yaml"
ENV_OUTPUT_DIR = "OUTPUT_DIR"
RUN_SUBDIR = "delivery_sample"
INPUT_SUBDIR = "input"
LINEAGE_NAME = lineage_utils.LINEAGE_NAME
STAGE_CONTEXT_NAME = lineage_utils.STAGE_CONTEXT_NAME

OVERRIDE_WHITELIST = ("max_samples_per_route", "sampling_seed")

#: 包级元数据中表示「可交付」的取值
_DELIVERABLE_TOKENS = {"deliverable", "可交付", "delivered", "packaged", "ready"}
_STATUS_BY_STATE = {"deliverable": sampling.ROUTE_SOURCE_STATUS,
                    "pending_release": "待发布", "pending": "待发布"}
#: 只有这些格式/字段才被视为「#23 交付包的版本状态声明」
_PACKAGE_MANIFEST_FORMAT = "crossmodal-delivery-package-v1"
_PACKAGE_VERSION_FORMAT = "crossmodal-delivery-version-v1"
_STATE_STATUS_TOKENS = {"可交付", "待发布", "deliverable", "pending_release", "pending",
                        "delivered", "packaged", "ready"}


class InputError(ValueError):
    """输入不合法（不存在、格式不支持、越出任务目录、包结构错误）。"""


# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #

def operator_dir() -> Path:
    return OPERATOR_DIR


def default_output_dir() -> Path:
    """输出根目录：环境变量 ``OUTPUT_DIR``，否则为算子目录下的 ``output/``。"""
    raw = os.environ.get(ENV_OUTPUT_DIR)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return OPERATOR_DIR / "output"


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> schemas.OperatorConfig:
    """读取并校验配置；未知键/越界/表1 只读口径冲突都会显式失败。"""
    import yaml

    payload: Dict[str, Any] = {}
    if config_path:
        target = Path(config_path)
        if not target.is_file():
            raise InputError("配置文件不存在：%s" % target)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except UnicodeDecodeError as exc:
            raise InputError("配置文件不是 UTF-8：%s（%s）" % (target, exc))
        except yaml.YAMLError as exc:
            raise InputError("配置文件 YAML 解析失败：%s（%s）" % (target, exc))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise InputError("配置文件顶层必须是映射：%s" % target)
        payload = loaded
    elif DEFAULT_CONFIG_PATH.is_file():
        return load_config(str(DEFAULT_CONFIG_PATH), overrides)

    if overrides:
        unknown = sorted(set(overrides) - set(OVERRIDE_WHITELIST))
        if unknown:
            raise schemas.ConfigError(
                "请求覆盖项不在白名单内：%s（允许：%s）"
                % (", ".join(unknown), ", ".join(OVERRIDE_WHITELIST)))
        payload = dict(payload)
        payload.update({key: value for key, value in overrides.items()})
    try:
        return schemas.OperatorConfig(**payload)
    except ValidationError as exc:
        raise schemas.ConfigError("配置校验失败：%s" % exc)


def _next_run_id(task_dir: Path) -> Tuple[str, Path]:
    base = task_dir / RUN_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    index = 1
    while (base / ("run%03d" % index)).exists():
        index += 1
    run_id = "run%03d" % index
    run_dir = base / run_id
    run_dir.mkdir(parents=True)
    return run_id, run_dir


# --------------------------------------------------------------------------- #
# 输入包解析
# --------------------------------------------------------------------------- #

def _read_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = io_utils.read_json_file(path)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_like_package(block: Any) -> bool:
    """只有 #23 交付包的 manifest/version 才作为「来源包状态」的声明来源。"""
    if not isinstance(block, dict):
        return False
    if block.get("format") in {_PACKAGE_MANIFEST_FORMAT, _PACKAGE_VERSION_FORMAT}:
        return True
    return "version_state" in block and "artifacts" not in block


def _load_isolation_ids(meta: Dict[str, Any]) -> set:
    """读取包内 ``materials/isolation_list.jsonl`` 的样本 ID（只读，仅用于排除）。"""
    ids: set = set()
    for directory in meta.get("meta_dirs") or []:
        path = Path(directory) / "materials" / "isolation_list.jsonl"
        if not path.is_file():
            continue
        try:
            for line in io_utils.iter_decoded_lines(path):
                if not line.strip():
                    continue
                try:
                    import json

                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    value = payload.get("sample_id") or payload.get("id")
                    if isinstance(value, str) and value.strip():
                        ids.add(value.strip())
        except (UnicodeDecodeError, OSError):
            continue
    return ids


def _metadata_dirs(entry: Path) -> List[Path]:
    """包级元数据查找目录：入口文件所在目录，及其上一级。"""
    parent = entry.parent
    return [parent, parent.parent]


def _collect_package_meta(entry: Path) -> Dict[str, Any]:
    """读取 #23 交付包的 manifest.json / version.json / materials/version_notes.json。"""
    meta: Dict[str, Any] = {"manifest": {}, "version": {}, "version_notes": {},
                            "meta_dirs": [], "declared_source": None}
    for directory in _metadata_dirs(entry):
        manifest = _read_json_if_exists(directory / transport.MANIFEST_NAME)
        version = _read_json_if_exists(directory / "version.json")
        notes = _read_json_if_exists(directory / "materials" / "version_notes.json")
        if manifest or version or notes:
            meta["manifest"] = meta["manifest"] or manifest
            meta["version"] = meta["version"] or version
            meta["version_notes"] = meta["version_notes"] or notes
            meta["meta_dirs"].append(str(directory))
        if meta["manifest"] and meta["version"]:
            break

    manifest, version, notes = meta["manifest"], meta["version"], meta["version_notes"]
    declared = None
    source = None
    for block, block_source in ((manifest, "package_manifest"),
                                (version, "package_version")):
        if not _looks_like_package(block):
            continue
        state = block.get("version_state")
        if isinstance(state, str) and state.strip():
            declared, source = state.strip(), block_source + ".version_state"
            break
        status = block.get("status")
        if isinstance(status, str) and status.strip().lower() in _STATE_STATUS_TOKENS:
            declared, source = status.strip(), block_source + ".status"
            break
    meta["declared_source"] = source
    meta["declared_state"] = declared
    meta["status_text"] = None
    for block in (version, manifest):
        if _looks_like_package(block) and isinstance(block.get("status"), str) \
                and block["status"].strip():
            meta["status_text"] = block["status"].strip()
            break
    if meta["status_text"] is None and declared:
        meta["status_text"] = _STATUS_BY_STATE.get(declared.lower(), "未声明")
    if meta["status_text"] is None:
        meta["status_text"] = "未声明"

    routes: Sequence[str] = ()
    for block in (version, notes, manifest):
        if isinstance(block, dict) and isinstance(block.get("enabled_routes"), list):
            routes = [route for route in block["enabled_routes"]
                      if sampling.normalize_route(route)]
            if routes:
                break
    meta["enabled_routes"] = [sampling.normalize_route(item) for item in routes]

    dataset_version = None
    for block in (version, notes, manifest):
        if isinstance(block, dict) and isinstance(block.get("dataset_version"), str):
            dataset_version = block["dataset_version"].strip()
            if dataset_version:
                break
    meta["dataset_version"] = dataset_version

    granularity = None
    for block in (version, notes):
        if isinstance(block, dict) and isinstance(block.get("annotation_granularity"), str):
            granularity = block["annotation_granularity"].strip()
            if granularity:
                break
    meta["annotation_granularity"] = granularity
    return meta


def _extract_zip_package(archive: Path, task_dir: Path) -> Path:
    """安全解压上传的交付包，返回解压目录（同一包按 sha256 复用）。"""
    digest = io_utils.file_sha256(archive)[:12]
    target = task_dir / INPUT_SUBDIR / ("pkg_" + digest)
    marker = target / ".extracted"
    if marker.is_file():
        return target
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    try:
        io_utils.safe_extract_zip(archive, target)
    except (ValueError, OSError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise InputError("交付包解压失败：%s" % exc)
    marker.write_text(digest + "\n", encoding="utf-8")
    return target


def _prepare_input(input_path: Path, task_dir: Path) -> Dict[str, Any]:
    """确定真正要读取的入口文件与容器类型（zip / directory）。"""
    if not input_path.exists():
        raise InputError("输入不存在：%s" % input_path)

    handoff_root = input_path if input_path.is_dir() else input_path.parent
    if input_path.suffix.lower() != ".zip":
        prepared = _prepare_handoff(handoff_root, input_path, task_dir)
        if prepared is not None:
            return prepared

    if input_path.is_dir():
        manifest = _read_json_if_exists(input_path / transport.MANIFEST_NAME)
        declared = manifest.get("entrypoint") if isinstance(manifest, dict) else None
        if isinstance(declared, str) and declared.strip():
            candidate = (input_path / declared.strip().replace("\\", "/")).resolve()
            if candidate.is_file():
                input_path = candidate
        if input_path.is_dir():
            top_level = sorted(
                path for path in input_path.iterdir()
                if path.is_file() and path.suffix.lower() == ".jsonl")
            usable = [path for path in top_level
                      if not transport._is_material_list(path.name)]
            if len(usable) == 1:
                input_path = usable[0]
            elif len(usable) > 1:
                raise InputError("目录中存在多个候选入口 JSONL，请显式指定：%s"
                                 % ", ".join(path.name for path in usable))
            else:
                archives = sorted(path for path in input_path.iterdir()
                                  if path.is_file() and path.suffix.lower() == ".zip")
                if len(archives) == 1:
                    input_path = archives[0]
                elif not archives:
                    raise InputError("目录中找不到交付包入口（JSONL 或 ZIP）：%s"
                                     % input_path)
                else:
                    raise InputError("目录中存在多个交付包 ZIP，请显式指定：%s"
                                     % ", ".join(path.name for path in archives))

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        extracted = _extract_zip_package(input_path, task_dir)
        prepared = _prepare_handoff(extracted, input_path, task_dir)
        if prepared is not None:
            return prepared
        try:
            entry, relative = transport.resolve_entrypoint(extracted)
        except transport.TransportError as exc:
            raise InputError("交付包内入口解析失败：%s" % exc)
        return {"entry": entry, "entrypoint": relative, "container": "zip",
                "input_file": str(input_path),
                "extracted_dir": extracted}

    if not input_path.is_file():
        raise InputError("输入不是文件也不是目录：%s" % input_path)
    if input_path.suffix.lower() not in {".jsonl", ".ndjson", ".json"}:
        raise InputError("输入格式不支持（需要 .jsonl / .json / .zip）：%s" % input_path)
    return {"entry": input_path, "entrypoint": input_path.name,
            "container": "directory", "input_file": str(input_path),
            "extracted_dir": None}


def _prepare_handoff(root: Path, original: Path, task_dir: Path):
    handoff = delivery_handoff.load(root)
    if handoff is None:
        manifest = _read_json_if_exists(root / transport.MANIFEST_NAME)
        declarations = [item for key in ("artifacts", "files")
                        for item in (manifest.get(key) or [])]
        archive_declared = any(
            str(item.get("path") if isinstance(item, dict) else item).lower().endswith(".zip")
            for item in declarations)
        if manifest.get("format") != _PACKAGE_MANIFEST_FORMAT and archive_declared:
            raise InputError("包装交付包缺少显式 delivery_handoff 清单，不能推测内部 ZIP")
        return None
    effects = delivery_handoff.review_effects(root, handoff)
    archive = delivery_handoff.safe_path(root, handoff["archive"]["path"])
    native = _extract_zip_package(archive, task_dir)
    try:
        entry, relative = transport.resolve_entrypoint(native)
    except transport.TransportError as exc:
        raise InputError("显式交付包入口解析失败：%s" % exc) from exc
    return {"entry": entry, "entrypoint": relative, "container": "zip-handoff",
            "input_file": str(original), "extracted_dir": native,
            "review_effects": effects}


# --------------------------------------------------------------------------- #
# 报表
# --------------------------------------------------------------------------- #

_BOUNDARY_NOTES = [
    "只从可交付语料包抽样；隔离区（sample_isolation_library）样本一律不参与。",
    "每条已开启路线最多抽 10 条；不足 10 条按实际条数导出并在报告中写明。",
    "样例生成失败不拦样本、不改用隔离区数据，只在质量报告/本报告中记载。",
    "本算子不生成预览页、波形图或 HTML；产物为 Excel 样例清单与报告。",
    "评分结论与标注结论均照实搬运上游证据，取不到写“未提供”，不产生任何分数。",
    "每条样例都标记 human_verified=false / review_required=true，等待人工抽检确认。",
]


def _report_pairs(report: Dict[str, Any], statistics: Dict[str, Any],
                  config: schemas.OperatorConfig) -> List[List[Any]]:
    """Excel「样例报告」工作表的项目/取值对（验收人员打开即可看全一版情况）。"""
    package = report.get("source") or {}
    pairs: List[List[Any]] = [
        ["算子", "%s（#%d %s，%s）" % (sampling.OPERATOR_LABEL, sampling.OPERATOR_NUMBER,
                                      sampling.OPERATOR, sampling.OPERATOR_VERSION)],
        ["环节", "%s（表1 第 8 环节：交付样例）" % sampling.STAGE_ID],
        ["任务标识 / 运行编号", "%s / %s" % (report.get("uniqueId"),
                                             report.get("runId"))],
        ["生成时间", report.get("generated_at")],
        ["来源包（容器）", package.get("container")],
        ["来源包入口", package.get("entrypoint")],
        ["来源包版本状态", package.get("version_state") or "未声明"],
        ["来源包状态", package.get("status")],
        ["数据版本", package.get("dataset_version") or "未声明"],
        ["启用路线", "、".join(package.get("enabled_routes") or []) or "未声明"],
        ["表1 每路线上限", "%d 条" % sampling.TABLE1_MAX_SAMPLES_PER_ROUTE],
        ["实际每路线上限", "%d 条" % config.max_samples_per_route],
        ["抽样模式 / 种子", "%s / %s" % (config.sampling_mode, config.sampling_seed)],
        ["抽样指纹（sha256）", statistics.get("sampling_fingerprint") or ""],
        ["输入记录数", statistics.get("input_records")],
        ["可用记录数", statistics.get("valid_records")],
        ["样例条数", statistics.get("sampled_records")],
        ["运行状态", report.get("status")],
        ["样例生成失败", "是（原因：%s）" % report.get("degrade_reason")
         if report.get("sample_generation_failed") else "否"],
        ["守恒校验", "通过" if statistics.get("conservation_ok") else "未通过"],
    ]
    insufficient = report.get("routes_insufficient") or []
    detail = "；".join(
        "%s（%s）：候选 %s 条 < 上限 %s 条（按实际条数导出）"
        % (route, sampling.route_modal_label(route),
           (report["routes"][route] or {}).get("available"),
           (report["routes"][route] or {}).get("limit"))
        for route in insufficient) or "无（所有已开启路线均达到上限）"
    pairs.append(["路线不足", detail])
    for route in sampling.ROUTE_ORDER:
        item = (report.get("routes") or {}).get(route)
        if not item:
            continue
        pairs.append(["路线：%s（%s）" % (sampling.route_modal_label(route), route),
                      "候选 %s 条；已抽 %s 条；上限 %s 条；%s"
                      % (item.get("available"), item.get("sampled"), item.get("limit"),
                         "不足上限" if item.get("insufficient") else "已达上限")])
    pairs.append(["边界口径", " ".join(_BOUNDARY_NOTES[:3])])
    pairs.append(["已知限制", "仅输出样例清单与报告；不做预览页/波形图/HTML；"
                              "不生成任何分数。"])
    return pairs


def _report_markdown(report: Dict[str, Any], config: schemas.OperatorConfig) -> str:
    lines = ["# 生成交付样例报告（#26）", "",
             "- 任务标识：`%s`" % report.get("uniqueId"),
             "- 运行编号：`%s`" % report.get("runId"),
             "- 生成时间：%s" % report.get("generated_at"),
             "- 运行状态：**%s**" % report.get("status"), "",
             "## 来源包", "",
             "| 项目 | 取值 |", "| --- | --- |",
             "| 容器 | %s |" % (report.get("source") or {}).get("container"),
             "| 入口 | %s |" % (report.get("source") or {}).get("entrypoint"),
             "| 版本状态 | %s |" % ((report.get("source") or {}).get("version_state")
                                    or "未声明"),
             "| 状态 | %s |" % (report.get("source") or {}).get("status"),
             "| 启用路线 | %s |" % ("、".join((report.get("source") or {})
                                              .get("enabled_routes") or []) or "未声明"),
             "", "## 抽样结果", "",
             "| 路线 | 候选 | 已抽 | 上限 | 是否不足 |", "| --- | --- | --- | --- | --- |"]
    for route in sampling.ROUTE_ORDER:
        item = (report.get("routes") or {}).get(route)
        if not item:
            continue
        lines.append("| %s（%s） | %s | %s | %s | %s |"
                     % (sampling.route_modal_label(route), route, item.get("available"),
                        item.get("sampled"), item.get("limit"),
                        "是" if item.get("insufficient") else "否"))
    lines += ["",
              "- 样例条数：**%s** 条" % report.get("sampled_records"),
              "- 抽样模式/种子：%s / %s" % (config.sampling_mode, config.sampling_seed),
              "- 抽样指纹：`%s`" % (report.get("statistics") or {}
                                    .get("sampling_fingerprint")),
              "- 守恒校验：%s" % ("通过" if (report.get("statistics") or {})
                                  .get("conservation_ok") else "未通过"), ""]
    if report.get("routes_insufficient"):
        lines += ["## 路线不足说明", ""]
        for route in report["routes_insufficient"]:
            item = report["routes"][route]
            lines.append("- %s（%s）：候选 %s 条，少于上限 %s 条，已按实际条数导出。"
                         % (sampling.route_modal_label(route), route,
                            item.get("available"), item.get("limit")))
        lines.append("")
    lines += ["## 边界与口径", ""]
    lines += ["- %s" % note for note in _BOUNDARY_NOTES]
    lines += ["", "## 产物", ""]
    for name, path in sorted((report.get("artifacts") or {}).items()):
        lines.append("- %s：`%s`" % (name, Path(path).name))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(*, input_file: Any, unique_id: Any, config_path: Optional[str] = None,
        output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True) -> Dict[str, Any]:
    """执行一次算子 26 运行，返回统一结果字典（含真实统计与产物路径）。"""
    task_id = io_utils.validate_unique_id(unique_id)
    config = load_config(config_path, overrides)

    root = Path(output_root).expanduser() if output_root else default_output_dir()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    task_dir = (root / task_id).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)

    raw_input = Path(str(input_file)).expanduser()
    input_path = raw_input.resolve()
    if not allow_external_input:
        if input_path != task_dir and task_dir not in input_path.parents:
            raise InputError("输入必须位于任务目录内：%s" % input_path)

    prepared = _prepare_input(input_path, task_dir)
    started_at = lineage_utils.now_iso()

    with io_utils.TaskLock(root, task_id):
        run_id, run_dir = _next_run_id(task_dir)
        log_path = run_dir / sampling.LOG_NAME
        with logger_utils.RunLogger(log_path) as run_logger:
            run_logger.event("run_start", "生成交付样例",
                             uniqueId=task_id, runId=run_id,
                             container=prepared["container"],
                             enabled=str(config.effective_enabled()))
            try:
                result = _process(task_id=task_id, run_id=run_id, run_dir=run_dir,
                                  prepared=prepared, config=config, task_dir=task_dir,
                                  started_at=started_at, run_logger=run_logger)
            except Exception as exc:                     # noqa: BLE001
                run_logger.event("run_error", type(exc).__name__,
                                 detail=str(exc)[:120])
                raise
            run_logger.event("run_finish", result["status"],
                             sampled=result["statistics"]["sampled_records"],
                             excel=str(result["statistics"]["excel_entries"]))
    return result


def _process(*, task_id: str, run_id: str, run_dir: Path, prepared: Dict[str, Any],
             config: schemas.OperatorConfig, task_dir: Path, started_at: str,
             run_logger: logger_utils.RunLogger) -> Dict[str, Any]:
    entry: Path = prepared["entry"]
    meta = _collect_package_meta(entry)

    declared = meta.get("declared_state")
    if declared:
        source_deliverable = str(declared).strip().lower() in _DELIVERABLE_TOKENS
        source_evidence = meta.get("declared_source")
    else:
        source_deliverable = not config.require_deliverable_source
        source_evidence = "record_markers"
    source_status = meta.get("status_text") or "未声明"

    review_effects = prepared.get("review_effects") or {}
    if review_effects.get("blockers"):
        source_deliverable = False
        source_evidence = "subsequent_review_pending"
        source_status = "待人工/待回写"

    isolation_ids = _load_isolation_ids(meta)
    isolation_ids.update(review_effects.get("excluded_ids") or [])
    package_routes = [route for route in (meta.get("enabled_routes") or []) if route]
    if config.route_source == "config" or not package_routes:
        enabled_routes = config.enabled_routes()
        route_source_used = "config"
    else:
        enabled_routes = [route for route in sampling.ROUTE_ORDER
                          if route in set(package_routes)]
        route_source_used = "package"

    package = {"container": prepared["container"],
               "entrypoint": prepared["entrypoint"],
               "version_state": declared,
               "status": source_status,
               "dataset_version": meta.get("dataset_version"),
               "annotation_granularity": meta.get("annotation_granularity"),
               "enabled_routes": list(enabled_routes),
               "declared_enabled_routes": list(package_routes),
               "route_source": route_source_used,
               "deliverable": bool(source_deliverable),
               "deliverable_evidence": source_evidence,
               "subsequent_review_blockers": review_effects.get("blockers") or [],
               "meta_dirs": meta.get("meta_dirs") or []}

    run_logger.event("package_loaded", meta.get("declared_source") or "no_package_meta",
                     entrypoint=prepared["entrypoint"], status=source_status,
                     routes=",".join(enabled_routes) or "none")

    statistics: Dict[str, Any] = {
        "operator": sampling.OPERATOR, "operator_number": sampling.OPERATOR_NUMBER,
        "operator_version": sampling.OPERATOR_VERSION, "stage_id": config.stage_id,
        "enabled": bool(config.effective_enabled()),
        "input_file": str(prepared["input_file"]), "entrypoint": prepared["entrypoint"],
        "package_container": prepared["container"],
        "package_version_state": declared, "package_status": source_status,
        "dataset_version": meta.get("dataset_version"),
        "source_status": source_status, "source_deliverable": bool(source_deliverable),
        "source_evidence": source_evidence,
        "model": None, "rule_based": True, "model_scores_generated": False,
        "human_verified": False,
        "enabled_routes": list(enabled_routes),
        "declared_enabled_routes": list(package_routes),
        "route_source": route_source_used,
        "input_records": 0, "valid_records": 0, "blank_lines": 0, "bad_lines": 0,
        "origin_quarantined": 0, "rejects_entries": 0,
        "sampled_records": 0, "route_disabled_records": 0,
        "route_exhausted_records": 0, "excluded_records": 0,
        "samples_jsonl_rows": 0,
        "bad_line_by_reason": {}, "excluded_by_reason": {},
        "route_disabled_by_reason": {}, "origin_by_reason": {},
        "route_available": {route: 0 for route in enabled_routes},
        "route_sampled": {route: 0 for route in enabled_routes},
        "route_insufficient": [], "max_samples_per_route": config.max_samples_per_route,
        "table1_max_samples_per_route": sampling.TABLE1_MAX_SAMPLES_PER_ROUTE,
        "sampling_mode": config.sampling_mode, "sampling_seed": config.sampling_seed,
        "sampling_fingerprint": None,
        "records_without_description": 0, "multi_route_records": 0,
        "excel_entries": 0, "excel_bytes": 0, "excel_sha256": None,
        "empty_input": False, "encoding_error": False, "file_read_incomplete": False,
        "sample_generation_failed": False, "degrade_reason": None,
    }

    event_time = lineage_utils.now_iso()
    main_path = run_dir / sampling.MAIN_NAME
    skipped_path = run_dir / sampling.SKIPPED_NAME
    excluded_path = run_dir / sampling.EXCLUDED_NAME
    rejects_path = run_dir / sampling.REJECTS_NAME
    pools: Dict[str, List[Dict[str, Any]]] = {route: [] for route in enabled_routes}
    selected: List[Dict[str, Any]] = []
    samples_rows: List[Dict[str, Any]] = []
    disabled = not config.effective_enabled()

    if disabled:
        run_logger.event("sampling_skipped", "operator_disabled_by_config")
        statistics["degrade_reason"] = "disabled_by_config"
        statistics["sample_generation_failed"] = True

    with io_utils.JsonlWriter(main_path) as main_writer, \
            io_utils.JsonlWriter(skipped_path) as skipped_writer, \
            io_utils.JsonlWriter(excluded_path) as excluded_writer, \
            io_utils.JsonlWriter(rejects_path) as rejects_writer:
        if not disabled:
            status = _stream_records(
                entry=entry, config=config, package=package,
                statistics=statistics, pools=pools,
                source_deliverable=source_deliverable,
                enabled_routes=enabled_routes, event_time=event_time,
                isolation_ids=isolation_ids,
                review_records=review_effects.get("records"),
                skipped_writer=skipped_writer, excluded_writer=excluded_writer,
                rejects_writer=rejects_writer, run_logger=run_logger)
            if status is not None:
                statistics["degrade_reason"] = status

            if source_deliverable:
                selected = _select_routes(pools, config=config, statistics=statistics,
                                          enabled_routes=enabled_routes,
                                          event_time=event_time,
                                          skipped_writer=skipped_writer)
                statistics["sampling_fingerprint"] = sampling.sampling_fingerprint(selected)
                for seq, item in enumerate(selected, start=1):
                    row = sampling.build_sample_row(item["payload"], item["spec"],
                                                    seq=seq, route=item["route"],
                                                    event_time=event_time,
                                                    package=package, config=config)
                    main_writer.write(row)
                    samples_rows.append(row)
            statistics["samples_jsonl_rows"] = main_writer.count

    statistics["sampled_records"] = len(samples_rows)
    statistics["records_without_description"] = sum(
        1 for row in samples_rows
        if not (row.get(sampling.NAMESPACE) or {}).get("description_summary"))
    statistics["rejects_entries"] = (statistics["bad_lines"]
                                     + statistics["origin_quarantined"])

    report = _finalize_status(statistics, config, source_deliverable=source_deliverable,
                              samples_rows=samples_rows, package=package,
                              task_id=task_id, run_id=run_id)
    number_ok, failures = sampling.check_conservation(statistics)
    statistics["conservation_ok"] = number_ok
    statistics["conservation_failures"] = failures
    if not number_ok:
        report["status"] = "failed"
        report["degrade_reason"] = "conservation_violation"
        report["sample_generation_failed"] = True
        statistics["degrade_reason"] = "conservation_violation"
        statistics["sample_generation_failed"] = True
    statistics["status"] = report["status"]
    report.update({"source": package, "statistics": statistics})

    # ---- Excel 样例清单 + 样例报告 ----
    excel_sheets = [
        {"name": "交付样例", "columns": list(sampling.EXCEL_COLUMNS),
         "rows": [sampling.excel_row(row, seq=index, package=package)
                  for index, row in enumerate(samples_rows, start=1)],
         "freeze_header": True, "autofilter": True},
        {"name": "样例报告", "columns": list(sampling.REPORT_COLUMNS),
         "rows": _report_pairs(report, statistics, config),
         "freeze_header": True, "autofilter": False},
    ]
    excel_path = run_dir / sampling.EXCEL_NAME
    excel_stats = xlsx.write_workbook(excel_path, excel_sheets)
    statistics["excel_entries"] = 1
    statistics["excel_bytes"] = excel_stats["bytes"]
    statistics["excel_sha256"] = excel_stats["sha256"]
    statistics["excel_sheets"] = excel_stats["sheets"]
    statistics["excel_data_rows"] = excel_stats["data_rows"]
    run_logger.event("excel_written", sampling.EXCEL_NAME,
                     rows=len(samples_rows), bytes=excel_stats["bytes"])

    report["excel"] = {"path": sampling.EXCEL_NAME, "bytes": excel_stats["bytes"],
                       "sha256": excel_stats["sha256"], "sheets": excel_stats["sheets"]}
    report_file = run_dir / sampling.REPORT_NAME
    io_utils.atomic_write_json(report_file, report)
    md_file = run_dir / sampling.REPORT_MD_NAME
    io_utils.atomic_write_text(md_file, _report_markdown(report, config))

    # ---- 清单（[v4.2] 血缘/阶段上下文输出已停用：不再构建血缘节点、不再写
    # lineage.json / stage_context.json，原代码整块注释存档） ----
    finished_at = lineage_utils.now_iso()
    artifacts = _artifact_entries(run_dir, samples_rows)
    # node = {
    #     "operator": sampling.OPERATOR,
    #     "operator_label": sampling.OPERATOR_LABEL,
    #     "operator_number": sampling.OPERATOR_NUMBER,
    #     "operator_version": sampling.OPERATOR_VERSION,
    #     "source_reference": sampling.SOURCE_REFERENCE,
    #     "stageId": config.stage_id,
    #     "uniqueId": task_id,
    #     "runId": run_id,
    #     "inputFile": str(prepared["input_file"]),
    #     "inputEntrypoint": prepared["entrypoint"],
    #     "outputFile": str(main_path),
    #     "status": report["status"],
    #     "startedAt": started_at,
    #     "finishedAt": finished_at,
    #     "config": lineage_utils.config_snapshot(config),
    #     "statistics": lineage_utils.statistics_snapshot(statistics),
    #     "artifacts": artifacts,
    #     "message": report.get("message"),
    # }
    # history = lineage_utils.read_lineage(run_dir / LINEAGE_NAME)
    # if not history and (task_dir / RUN_SUBDIR).exists():
    #     for previous in sorted((task_dir / RUN_SUBDIR).iterdir()):
    #         if previous.is_dir() and previous != run_dir:
    #             history.extend(lineage_utils.read_lineage(previous / LINEAGE_NAME))
    # lineage_file = run_dir / LINEAGE_NAME
    # lineage_utils.write_lineage(lineage_file, history + [node])
    #
    # context = {"format": "crossmodal-stage-context-v1",
    #            "stage_id": config.stage_id,
    #            "uniqueId": task_id, "runId": run_id,
    #            "operator": sampling.OPERATOR,
    #            "operator_number": sampling.OPERATOR_NUMBER,
    #            "operator_version": sampling.OPERATOR_VERSION,
    #            "status": report["status"],
    #            "inputFile": str(prepared["input_file"]),
    #            "outputFile": str(main_path),
    #            "package": package, "report": str(report_file),
    #            "startedAt": started_at, "finishedAt": finished_at}
    # context_file = run_dir / STAGE_CONTEXT_NAME
    # lineage_utils.write_stage_context(context_file, context)

    manifest = transport.build_manifest(
        sampling.MAIN_NAME,
        artifacts + [lineage_utils.artifact_entry(transport.MANIFEST_NAME, "manifest")],
        operator=sampling.OPERATOR, operator_number=sampling.OPERATOR_NUMBER,
        operator_version=sampling.OPERATOR_VERSION, uniqueId=task_id, runId=run_id,
        stage_id=config.stage_id, status=report["status"])
    manifest_path = transport.write_manifest(run_dir, manifest)

    file_object = transport.pack_file_object(
        run_dir, sampling.MAIN_NAME,
        [item["path"] for item in manifest["artifacts"]],
        max_response_bytes=config.transport.max_response_bytes)

    result = {
        "status": report["status"],
        "message": report.get("message"),
        "uniqueId": task_id, "runId": run_id,
        "runDir": str(run_dir),
        "inputFile": str(prepared["input_file"]),
        "outputFile": str(main_path),
        "excelFile": str(excel_path),
        "skippedFile": str(skipped_path),
        "excludedFile": str(excluded_path),
        "rejectsFile": str(rejects_path),
        "reportFile": str(report_file),
        "reportMarkdownFile": str(md_file),
        # [v4.2] 血缘输出已停用：不再回传 lineageFile/contextFile/node/lineage/
        # stageLineage 键（lineage.json / stage_context.json 不再写出）
        # "lineageFile": str(lineage_file),
        # "contextFile": str(context_file),
        "manifestPath": str(manifest_path),
        "logFile": str(run_dir / sampling.LOG_NAME),
        "artifacts": artifacts,
        "statistics": statistics,
        "report": report,
        # "node": node,
        # "lineage": history + [node],
        # "stageLineage": [dict(item) for item in history + [node]],
        "fileObject": file_object,
        "transport": {"format": transport.FILE_OBJECT_FORMAT,
                      "entrypoint": sampling.MAIN_NAME,
                      "max_response_bytes": config.transport.max_response_bytes,
                      "file_count": file_object["file_count"],
                      "total_bytes": file_object["total_bytes"]},
    }
    return result


# --------------------------------------------------------------------------- #
# 逐条流式处理
# --------------------------------------------------------------------------- #

def _bump(bucket: Dict[str, int], key: str, amount: int = 1) -> None:
    bucket[key] = bucket.get(key, 0) + amount


def _stream_records(*, entry: Path, config: schemas.OperatorConfig,
                    package: Dict[str, Any], statistics: Dict[str, Any],
                    pools: Dict[str, List[Dict[str, Any]]], source_deliverable: bool,
                    enabled_routes: Sequence[str], event_time: str,
                    isolation_ids: set,
                    review_records: Optional[Dict[str, Any]] = None,
                    skipped_writer: io_utils.JsonlWriter,
                    excluded_writer: io_utils.JsonlWriter,
                    rejects_writer: io_utils.JsonlWriter,
                    run_logger: logger_utils.RunLogger) -> Optional[str]:
    """逐行读取交付包，完成校验、分类与计数；返回需要覆盖的降级原因（或 ``None``）。"""
    import json

    seen_ids: set = set()
    line_number = 0
    iterator: Iterator[str] = io_utils.iter_decoded_lines(entry)
    encoding_failed = False
    while True:
        try:
            line = next(iterator)
        except StopIteration:
            break
        except UnicodeDecodeError as exc:
            encoding_failed = True
            line_number += 1
            statistics["encoding_error"] = True
            statistics["file_read_incomplete"] = True
            statistics["bad_lines"] += 1
            _bump(statistics["bad_line_by_reason"], sampling.REASON_ENCODING)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_ENCODING,
                detail="第 %d 行起出现非 UTF-8 字节：%s" % (line_number, exc.__class__.__name__),
                detected_at=event_time))
            break

        line_number += 1
        raw = line.rstrip("\r")
        if not raw.strip():
            statistics["blank_lines"] += 1
            continue
        statistics["input_records"] += 1

        identifier: Optional[str] = None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            statistics["bad_lines"] += 1
            _bump(statistics["bad_line_by_reason"], sampling.REASON_JSON_PARSE)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_JSON_PARSE,
                detail="JSON 解析失败：%s" % exc.__class__.__name__,
                detected_at=event_time, raw_text=raw))
            continue
        if not isinstance(payload, dict):
            statistics["bad_lines"] += 1
            _bump(statistics["bad_line_by_reason"], sampling.REASON_NOT_OBJECT)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_NOT_OBJECT,
                detail="JSONL 记录必须是对象，实际为 %s" % type(payload).__name__,
                detected_at=event_time, raw_text=raw[:200]))
            continue

        if review_records is not None:
            source_id = payload.get("sample_id")
            if source_id in review_records:
                payload = review_records[source_id]
            elif isinstance(source_id, str):
                isolation_ids.add(source_id)

        spec = sampling.resolve_record(payload, enabled_routes=enabled_routes,
                                      isolation_ids=isolation_ids,
                                      description_chars=config.description_summary_chars)
        identifier = spec["identifier"]

        if not identifier:
            statistics["origin_quarantined"] += 1
            _bump(statistics["origin_by_reason"], sampling.REASON_MISSING_IDENTIFIER)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_MISSING_IDENTIFIER,
                detail="记录缺少 sample_id（表1 #9 要求样本 ID 唯一且非空）",
                detected_at=event_time, raw_text=raw))
            continue
        if identifier in seen_ids:
            statistics["origin_quarantined"] += 1
            _bump(statistics["origin_by_reason"], sampling.REASON_DUPLICATE_IDENTIFIER)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_DUPLICATE_IDENTIFIER,
                detail="样本 ID 重复：%s" % identifier, detected_at=event_time,
                identifier=identifier))
            continue
        seen_ids.add(identifier)

        if spec["unsafe_refs"]:
            statistics["origin_quarantined"] += 1
            _bump(statistics["origin_by_reason"], sampling.REASON_UNSAFE_MEDIA_PATH)
            rejects_writer.write(sampling.make_reject_entry(
                line=line_number, reason=sampling.REASON_UNSAFE_MEDIA_PATH,
                detail="媒体引用不合法（绝对路径/目录穿越/网络地址）：%s"
                       % spec["unsafe_refs"][0][:80],
                detected_at=event_time, identifier=identifier))
            continue

        statistics["valid_records"] += 1

        isolation = spec["isolation"]
        if isolation is not None:
            statistics["excluded_records"] += 1
            _bump(statistics["excluded_by_reason"], sampling.REASON_UPSTREAM_ISOLATED)
            excluded_writer.write(sampling.make_ledger_entry(
                line=line_number, payload=payload, action="exclude",
                label=isolation.get("label") or sampling.LABEL_ISOLATED,
                destination=sampling.DEST_SAMPLE_ISOLATION,
                reason_code=sampling.REASON_UPSTREAM_ISOLATED,
                detail=isolation.get("detail"), event_time=event_time,
                max_embedded_bytes=config.max_embedded_record_bytes,
                extra={"isolation_source": isolation.get("source")}))
            continue

        if not source_deliverable:
            statistics["excluded_records"] += 1
            _bump(statistics["excluded_by_reason"], "source_not_deliverable")
            excluded_writer.write(sampling.make_ledger_entry(
                line=line_number, payload=payload, action="exclude",
                label=sampling.LABEL_NOT_DELIVERABLE,
                destination=sampling.DEST_SAMPLE_POOL,
                reason_code="source_not_deliverable",
                detail="来源包未标记为可交付（表1 #26：只从可交付包抽样），"
                       "本记录不参与抽样也不进入样例清单",
                event_time=event_time,
                max_embedded_bytes=config.max_embedded_record_bytes))
            continue

        marker = spec["delivery"]
        if config.require_deliverable_record and marker not in _DELIVERABLE_TOKENS:
            statistics["excluded_records"] += 1
            _bump(statistics["excluded_by_reason"], sampling.REASON_NOT_DELIVERABLE)
            excluded_writer.write(sampling.make_ledger_entry(
                line=line_number, payload=payload, action="exclude",
                label=sampling.LABEL_NOT_DELIVERABLE,
                destination=sampling.DEST_SAMPLE_POOL,
                reason_code=sampling.REASON_NOT_DELIVERABLE,
                detail="记录未携带 #23 可交付标记（decision=%s）" % (marker or "缺失"),
                event_time=event_time,
                max_embedded_bytes=config.max_embedded_record_bytes))
            continue

        route = spec["route"]
        if route is None:
            statistics["route_disabled_records"] += 1
            reason = (sampling.REASON_ROUTE_NOT_ENABLED if spec["routes"]
                      else sampling.REASON_NO_ROUTE)
            _bump(statistics["route_disabled_by_reason"], reason)
            skipped_writer.write(sampling.make_ledger_entry(
                line=line_number, payload=payload, action="skip",
                label=sampling.LABEL_SKIPPED, destination=sampling.DEST_SAMPLE_POOL,
                reason_code=reason,
                detail=("记录路线 %s 不在已开启路线 %s 内"
                        % (spec["routes"], list(enabled_routes))
                        if spec["routes"] else "记录没有可映射的模态路线"),
                event_time=event_time,
                max_embedded_bytes=config.max_embedded_record_bytes))
            continue

        pools.setdefault(route, []).append({"identifier": identifier, "route": route,
                                            "payload": payload, "spec": spec,
                                            "line": line_number})
        statistics["route_available"][route] = \
            statistics["route_available"].get(route, 0) + 1
        if spec["multi_route"]:
            statistics["multi_route_records"] += 1

    if encoding_failed:
        return "file_read_incomplete"
    if statistics["input_records"] and not statistics["valid_records"]:
        return "all_records_unusable"
    if statistics["input_records"] == 0:
        statistics["empty_input"] = True
        run_logger.event("empty_input", "交付包入口为空")
        return "no_deliverable_sample"
    return None


def _select_routes(pools: Dict[str, List[Dict[str, Any]]],
                   *, config: schemas.OperatorConfig, statistics: Dict[str, Any],
                   enabled_routes: Sequence[str], event_time: str,
                   skipped_writer: io_utils.JsonlWriter) -> List[Dict[str, Any]]:
    """每条已开启路线抽至多 N 条；超出的记录进 skipped 台账（不静默丢弃）。"""
    selected: List[Dict[str, Any]] = []
    for route in enabled_routes:
        pool = pools.get(route, [])
        picked = sampling.select_samples(pool, max_samples=config.max_samples_per_route,
                                         mode=config.sampling_mode,
                                         seed=config.sampling_seed)
        statistics["route_sampled"][route] = len(picked)
        statistics["route_available"][route] = len(pool)
        if len(pool) < config.max_samples_per_route:
            statistics["route_insufficient"].append(route)
        picked_ids = {item["identifier"] for item in picked}
        for item in pool:
            if item["identifier"] in picked_ids:
                continue
            statistics["route_exhausted_records"] += 1
            skipped_writer.write(sampling.make_ledger_entry(
                line=item["line"], payload=item["payload"], action="skip",
                label=sampling.LABEL_SKIPPED, destination=sampling.DEST_SAMPLE_POOL,
                reason_code=sampling.REASON_QUOTA_EXHAUSTED,
                detail="路线 %s 已达到每路线上限 %d 条，本记录未抽入样例清单"
                       % (route, config.max_samples_per_route),
                event_time=event_time,
                max_embedded_bytes=config.max_embedded_record_bytes,
                extra={"route": route}))
        selected.extend(picked)
    order = {route: index for index, route in enumerate(sampling.ROUTE_ORDER)}
    return sorted(selected, key=lambda item: (order.get(item["route"], 99),
                                              item["identifier"]))


def _finalize_status(statistics: Dict[str, Any], config: schemas.OperatorConfig,
                     *, source_deliverable: bool, samples_rows: List[Dict[str, Any]],
                     package: Dict[str, Any], task_id: str,
                     run_id: str) -> Dict[str, Any]:
    """决定运行状态与降级原因，并给出 Human 可读信息。"""
    status = "success"
    reason = statistics.get("degrade_reason")
    message = None

    if not config.effective_enabled():
        status, reason = "degraded", "disabled_by_config"
        message = "算子 26 已在配置中关闭（可选算子），本次未生成样例清单。"
    elif reason == "all_records_unusable":
        status = "failed"
        message = "交付包内没有任何可用记录（全部为坏行/缺标识/非法引用），无法生成样例。"
    elif statistics.get("conservation_failures"):
        status, reason = "failed", "conservation_violation"
        message = "统计守恒校验未通过：%s" % "；".join(
            statistics["conservation_failures"])
    elif not source_deliverable:
        status = "failed" if config.require_deliverable_source else "degraded"
        reason = "source_not_deliverable"
        message = ("来源包版本状态为 %s（非可交付），按表1 #26 不从中抽样；"
                   "也未改用隔离区数据。" % (package.get("version_state") or "未声明"))
    elif statistics["sampled_records"] == 0:
        status, reason = "degraded", "no_deliverable_sample"
        message = "已开启路线 %s 中没有可交付样本，样例清单为空。" % (
            ", ".join(statistics.get("enabled_routes") or []) or "（无）")
    if status == "success" and statistics.get("route_insufficient"):
        message = ("样例清单已生成：%d 条；其中路线 %s 候选不足上限，已按实际条数导出。"
                   % (statistics["sampled_records"],
                      ", ".join(statistics["route_insufficient"])))
    if status == "success" and not message:
        message = "样例清单已生成：%d 条。" % statistics["sampled_records"]

    report = {
        "format": "crossmodal-delivery-sample-report-v1",
        "operator": sampling.OPERATOR,
        "operator_label": sampling.OPERATOR_LABEL,
        "operator_number": sampling.OPERATOR_NUMBER,
        "operator_version": sampling.OPERATOR_VERSION,
        "source_reference": sampling.SOURCE_REFERENCE,
        "stage_id": config.stage_id,
        "uniqueId": task_id, "runId": run_id,
        "generated_at": lineage_utils.now_iso(),
        "status": status,
        "message": message,
        "degrade_reason": reason,
        "sample_generation_failed": bool(statistics.get("sampled_records") == 0),
        "table1_max_samples_per_route": sampling.TABLE1_MAX_SAMPLES_PER_ROUTE,
        "max_samples_per_route": config.max_samples_per_route,
        "sampling_mode": config.sampling_mode, "sampling_seed": config.sampling_seed,
        "sampled_records": statistics["sampled_records"],
        "route_source": statistics.get("route_source"),
        "routes": {}, "routes_insufficient": list(statistics["route_insufficient"]),
        "boundary": list(_BOUNDARY_NOTES),
        "artifacts": {"main": sampling.MAIN_NAME, "excel": sampling.EXCEL_NAME,
                      "skipped": sampling.SKIPPED_NAME,
                      "excluded": sampling.EXCLUDED_NAME,
                      "rejects": sampling.REJECTS_NAME,
                      "report": sampling.REPORT_NAME,
                      "reportMarkdown": sampling.REPORT_MD_NAME,
                      "log": sampling.LOG_NAME,
                      # [v4.2] 血缘输出已停用：不再登记 lineage / stage_context 产物
                      # "lineage": LINEAGE_NAME,
                      # "stageContext": STAGE_CONTEXT_NAME,
                      },
        "notes": [],
    }
    for route in sampling.ROUTE_ORDER:
        item = statistics["route_sampled"].get(route)
        if item is None:
            continue
        report["routes"][route] = {
            "route": route, "modal": sampling.route_modal_label(route),
            "available": statistics["route_available"].get(route, 0),
            "sampled": item, "limit": config.max_samples_per_route,
            "exhausted": statistics["route_sampled"].get(route, 0)
            >= config.max_samples_per_route,
            "insufficient": route in statistics["route_insufficient"]}
    notes = list(_BOUNDARY_NOTES)
    if report["routes_insufficient"]:
        notes.append("路线不足上限：%s（已按实际条数导出，见 Excel「样例报告」）。"
                     % ", ".join(report["routes_insufficient"]))
    if statistics.get("encoding_error"):
        notes.append("交付包入口存在非 UTF-8 字节，已停止读取并登记为编码错误。")
    notes.append("抽样为大包上的确定性伪随机抽样（固定种子），同一输入可复现。")
    notes.append("评分/标注结论均为上游证据的照实搬运，本算子不产生任何分数。")
    report["notes"] = notes
    statistics["degrade_reason"] = reason
    statistics["sample_generation_failed"] = bool(report["sample_generation_failed"])
    return report


def _artifact_entries(run_dir: Path, samples_rows: List[Dict[str, Any]]
                      ) -> List[Dict[str, Any]]:
    roles = {
        sampling.MAIN_NAME: "records",
        sampling.EXCEL_NAME: "excel_sample_list",
        sampling.SKIPPED_NAME: "skip_ledger",
        sampling.EXCLUDED_NAME: "exclusion_ledger",
        sampling.REJECTS_NAME: "origin_quarantine",
        sampling.REPORT_NAME: "sample_report",
        sampling.REPORT_MD_NAME: "sample_report_markdown",
        sampling.LOG_NAME: "log",
        # [v4.2] 血缘输出已停用：不再登记 lineage.json 产物
        # LINEAGE_NAME: "lineage",
    }
    entries: List[Dict[str, Any]] = []
    for name, role in roles.items():
        path = run_dir / name
        if path.is_file():
            entries.append(lineage_utils.artifact_entry(name, role, path.stat().st_size))
    return entries


# --------------------------------------------------------------------------- #
# 三接口返回形态辅助
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：build_intermediate_lineage / build_stage_payload 不再被
# app.py 调用（run() 已不再返回 node/lineage/stageLineage），函数体注释存档。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """阶段中间返回：只给 ``lineageFile``（含当前临时 ``outputFile``）。"""
#     node = dict(result["node"])
#     node["statistics"] = lineage_utils.statistics_snapshot(result["statistics"])
#     return node
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末返回：整条阶段血缘（不含临时 ``outputFile``）。"""
#     payload: List[Dict[str, Any]] = []
#     for item in result.get("stageLineage") or []:
#         node = dict(item)
#         node.pop("outputFile", None)
#         payload.append(node)
#     return payload


def build_file_object(result: Dict[str, Any]) -> Dict[str, Any]:
    return result["fileObject"]


def envelope(code: int, msg: str = "", data: Optional[Dict[str, Any]] = None
             ) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data if data is not None else {}}
