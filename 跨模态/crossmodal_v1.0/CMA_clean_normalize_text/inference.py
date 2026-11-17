"""算子 3 文本清洗标准化与分词：配置加载、主处理流程与 CLI。

职责（DEVELOPMENT_PLAN §1）：
- 输入：S2 阶段首的 02_screened.jsonl（HTTP 不传 inputFile，由 app.py 按 manifest 定位）；
- 输出：03_text_cleaned.jsonl（主清单，每行完整记录）与 03_rejects.jsonl（旁路异常清单）；
- 媒体引用继续可读：把仍被有效记录引用的媒体复制进本次 run 目录并登记 manifest；
- 本算子只做文本，不做语言识别、不做图像/视频/音频处理。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
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

from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from pydantic import ValidationError

#: 内部别名，便于在记录处理中调用
_deepcopy = copy.deepcopy
_copyfile = shutil.copyfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import text_processing as tp  # noqa: E402
from utils.io import (JsonlWriter, TaskBusyError, TaskLock,  # noqa: E402
                      UnsupportedMediaPath, assert_utf8, atomic_write_json,
                      next_run_id, read_jsonl_stream, resolve_media, task_dir)
# [v4.2] 血缘/阶段归档已停用：make_lineage/append_lineage/build_stage_context/
# load_prior_lineage 不再导入与调用（utils/lineage.py 保留存档）；now_iso 仅为时间工具，继续保留。
from utils.lineage import now_iso  # noqa: E402
from utils.logger import close_logger, get_logger, log_action  # noqa: E402
from utils.schemas import (BUSINESS_CONFIG_KEYS, DEPLOYMENT_CONFIG_KEYS,  # noqa: E402
                           OperatorConfig, ensure_finite_numbers)
from utils.transport import build_manifest, write_manifest  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

MAIN_NAME = "03_text_cleaned.jsonl"
REJECT_NAME = "03_rejects.jsonl"
MANIFEST_NAME = "manifest.json"
OPERATOR_DIR_NAME = "text_normalization"
MEDIA_FIELDS = ("image", "video", "audio")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def resolve_output_root(cfg: OperatorConfig) -> Path:
    """输出根目录：``OUTPUT_DIR`` 环境变量优先，否则用配置 ``io.output_root``。"""
    return Path(os.getenv("OUTPUT_DIR") or str(BASE_DIR / cfg.io.output_root))


def verify_stage_media(entry_path, run_dir: Path) -> List[str]:
    """校验主清单里所有媒体引用在本次 run 目录内存在且非空。

    只做存在性与大小检查；可解码性由 04-06/21 的媒体算子负责。
    """
    problems: List[str] = []
    for _lineno, record, error in read_jsonl_stream(entry_path):
        if error:
            continue
        for ref, relative in collect_media_refs(record):
            try:
                target = resolve_media(run_dir, relative)
            except UnsupportedMediaPath:
                problems.append("%s:%s" % (record.get("sample_id"), ref))
                continue
            if not target.is_file() or target.stat().st_size == 0:
                problems.append("%s:%s" % (record.get("sample_id"), ref))
    return problems


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> Tuple[OperatorConfig, Dict[str, Any]]:
    """加载并校验配置。

    ``operator`` / ``stage_id`` / ``io`` / ``server`` 属部署配置：请求 ``overrides``
    中的这些键会被忽略，防止请求改写血缘身份或放宽服务端限额。
    """
    path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("配置文件无法读取：%s" % type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")

    merged: Dict[str, Any] = dict(raw)
    applied: List[str] = []
    ignored: List[str] = []
    for key, value in (overrides or {}).items():
        if key in DEPLOYMENT_CONFIG_KEYS:
            ignored.append(key)
            continue
        if key not in BUSINESS_CONFIG_KEYS:
            raise ValueError("表1不允许覆盖参数：%s" % key)
        merged[key] = value
        applied.append(key)

    ensure_finite_numbers(merged)
    cfg = OperatorConfig(**merged)

    full = cfg.model_dump()
    business = {key: full[key] for key in BUSINESS_CONFIG_KEYS if key in full}
    snapshot = {
        "business": business,
        "deployment": {
            "operator": full["operator"],
            "stage_id": full["stage_id"],
            "io": full["io"],
            "server": full["server"],
        },
        "overridden_keys": sorted(applied),
        "ignored_deployment_keys": sorted(ignored),
    }
    return cfg, snapshot


# --------------------------------------------------------------------------- #
# 记录处理
# --------------------------------------------------------------------------- #

def collect_media_refs(record: Dict[str, Any]) -> List[Tuple[str, str]]:
    """收集记录仍引用的媒体相对路径，返回 ``(引用位置, 相对路径)``。"""
    refs: List[Tuple[str, str]] = []
    for field in MEDIA_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            refs.append((field, value.strip()))
        elif isinstance(value, dict):
            for key in ("path", "original_path"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    refs.append(("%s.%s" % (field, key), item.strip()))
            if field == "video":
                for index, frame in enumerate(value.get("frames") or []):
                    if isinstance(frame, dict) and isinstance(frame.get("path"), str):
                        refs.append(("video.frames[%d]" % index, frame["path"]))
            if field == "audio":
                for index, segment in enumerate(value.get("segments") or []):
                    if isinstance(segment, dict) and isinstance(segment.get("path"), str):
                        refs.append(("audio.segments[%d]" % index, segment["path"]))
    return refs


def _incoming_text(record: Dict[str, Any]) -> Optional[str]:
    """取入口文本：字符串直接使用；对象优先 ``content``，回退 ``original``。"""
    value = record.get("text")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "original"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    return None


def _original_text(record: Dict[str, Any]) -> str:
    """保留最初 original：已存在则沿用，避免重复清洗后丢失审计原文。"""
    value = record.get("text")
    if isinstance(value, dict):
        for key in ("original", "content"):
            item = value.get(key)
            if isinstance(item, str):
                return item
        return ""
    return value if isinstance(value, str) else ""


def _build_text_object(record: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    previous = record.get("text")
    text_object: Dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    text_object["content"] = result["content"]
    text_object["original"] = text_object.get("original") or _original_text(record)
    if not text_object.get("lang"):
        text_object["lang"] = result["lang"]
    # 上游tokens/offset/tokenizer原样沿用；本工具不新增分词结果。
    return text_object


def _reject(sample_id: Optional[str], line: int, reason: str,
            record: Optional[dict] = None, evidence: Optional[dict] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "sample_id": sample_id,
        "operator": OPERATOR_DIR_NAME,
        "line": line,
        "reason": reason,
        "destination": "原始隔离区", "tag": "原始文件-编码异常",
    }
    if record is not None:
        entry["record"] = record
    if evidence:
        entry["evidence"] = evidence
    return entry


def _bump(stats: Dict[str, Any], key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def _add_reason(stats: Dict[str, Any], reason: str) -> None:
    _bump(stats, "reason:%s" % reason)


def _null_ref(record: Dict[str, Any], ref: str) -> None:
    """把无法搬运的媒体引用置为 null，避免留下指向不存在文件的活跃引用。"""
    if ref in MEDIA_FIELDS:
        record[ref] = None
        return
    field, _, key = ref.partition(".")
    value = record.get(field)
    if not isinstance(value, dict):
        return
    if key in ("path", "original_path"):
        value[key] = None
        return
    for name in ("frames", "segments"):
        prefix = name + "["
        if key.startswith(prefix) and key.endswith("]"):
            try:
                index = int(key[len(prefix):-1])
            except ValueError:
                return
            items = value.get(name)
            if isinstance(items, list) and 0 <= index < len(items) and isinstance(items[index], dict):
                items[index]["path"] = None
            return


def _carry_media(record: Dict[str, Any], media_root: Path, run_dir: Path,
                 copied: Dict[str, str], stats: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any], Dict[str, str]]:
    """校验并搬运记录引用的媒体；返回 (错误原因, 可搬运副本, 出错引用)。"""
    carried: Dict[str, Any] = _deepcopy(record)
    errors: Dict[str, str] = {}
    for ref, relative in collect_media_refs(record):
        try:
            source = resolve_media(media_root, relative)
        except UnsupportedMediaPath:
            errors[ref] = relative
            _null_ref(carried, ref)
            return "media_path_unsupported:%s" % ref, carried, errors
        if not source.is_file() or source.stat().st_size == 0:
            _bump(stats, "missing_media")
            errors[ref] = relative
            _null_ref(carried, ref)
            return "media_missing:%s" % ref, carried, errors
        if relative not in copied:
            destination = run_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copyfile(source, destination)
            copied[relative] = "media"
            _bump(stats, "media_copied")
    return None, carried, errors


def process_record(record: Dict[str, Any], line: int, business_cfg: Dict[str, Any],
                   media_root: Path, run_dir: Path, copied: Dict[str, str],
                   stats: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """处理单条有效记录，返回 ``(去向, 记录或异常条目)``。

    去向：``output``（主清单，含 not_applicable）、``reject``（异常清单）。
    """
    if not business_cfg.get("enabled", False):
        out = _deepcopy(record)
        for _, relative in collect_media_refs(record):
            try:
                source = resolve_media(media_root, relative)
                if source.is_file() and relative not in copied:
                    dest = run_dir / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _copyfile(source, dest)
                    copied[relative] = "media"
            except UnsupportedMediaPath:
                pass  # 关闭时不加工、不以媒体问题隔离；由后续门禁核对。
        _bump(stats, "not_applicable")
        return "output", out

    sample_id = record.get("sample_id")
    if sample_id is None:
        return "reject", _reject(None, line, "missing_field:sample_id", record=record)
    if not isinstance(sample_id, str) or not sample_id.strip():
        return "reject", _reject(None, line, "invalid_type:sample_id", record=record)

    processing = record.get("processing")
    if processing is not None and not isinstance(processing, dict):
        return "reject", _reject(sample_id, line, "invalid_type:processing", record=record)

    raw_text = record.get("text")
    if raw_text is not None and not isinstance(raw_text, (str, dict)):
        return "reject", _reject(sample_id, line, "invalid_type:text", record=record)

    text_input = _incoming_text(record)
    missing_description = (isinstance(text_input,str) and not text_input.strip()
                           and any(record.get(k) for k in ("image","video","audio")))
    if text_input is None or missing_description:
        reason = "missing_description" if missing_description else ("video_audio_pair" if (record.get("video") and record.get("audio")) else "no_text_field")
        carried_error, carried, errors = _carry_media(record, media_root, run_dir, copied, stats)
        if carried_error:
            return "reject", _reject(sample_id, line, carried_error, record=carried,
                                     evidence={"media_errors": errors})
        out = carried
        out.setdefault("processing", {})
        out["processing"]["03"] = {
            # [v4.2] 输出记录 status 双字段：int status（1=成功/通过）+ process_status 流程态
            "status": 1,
            "process_status": "not_applicable",
            "reason": reason,
            "metrics": {"chars_in": 0, "chars_out": 0, "tokens": 0,
                        "tokenizer": stats["tokenizer"], "degraded": bool(stats["degraded"])},
        }
        _bump(stats, "not_applicable")
        return "output", out

    try:
        result = tp.normalize_text(text_input, business_cfg)
    except tp.TokenizerUnavailableError:
        raise
    except (TypeError, ValueError, re.error) as exc:
        return "reject", _reject(sample_id, line, "text_normalize_error", record=record,
                                 evidence={"error": type(exc).__name__})

    carried_error, carried, errors = _carry_media(record, media_root, run_dir, copied, stats)
    if carried_error:
        return "reject", _reject(sample_id, line, carried_error, record=carried,
                                 evidence={"media_errors": errors})

    metrics = result["metrics"]
    if result["content"] == "":
        reason = "empty_text_after_cleaning"
        return "reject", _reject(sample_id, line, reason, record=carried,
                                 evidence={"chars_in": metrics["chars_in"]})
    if not any(ch.isalnum() for ch in result["content"]):
        return "reject", _reject(sample_id, line, "unreadable_text", record=carried)

    out = carried
    out["text"] = _build_text_object(record, result)
    out.setdefault("processing", {})
    # [v4.2] 输出记录 status 双字段：int status（1=成功）+ process_status 流程态
    out["processing"]["03"] = {"status": 1, "process_status": "success", "reason": None,
                               "metrics": _public_metrics(metrics)}
    if result["content"] != _original_text(record):
        _bump(stats, "text_cleaned")
    else:
        _bump(stats, "text_unchanged")
    for key in ("html_tags_removed", "script_style_removed", "control_chars_removed",
                "invisible_chars_removed", "whitespace_normalized", "normalization_changes",
                "replacement_chars", "lowercased", "punctuation_normalized", "tokens"):
        _bump(stats, key, int(metrics.get(key) or 0))
    for pattern, count in (metrics.get("ad_patterns_removed") or {}).items():
        stats["ad_patterns_removed"][pattern] = stats["ad_patterns_removed"].get(pattern, 0) + count
    return "output", out


def _public_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(metrics)
    payload.pop("lang_source", None)
    return payload


# --------------------------------------------------------------------------- #
# 结果装配
# --------------------------------------------------------------------------- #

def _result(status: str, output_file: Optional[str], lineage_file: str,
            statistics: Dict[str, Any], message: str, run_id: Optional[str],
            run_dir: Optional[str], entrypoint: str, rejects_file: Optional[str],
            manifest_file: Optional[str], stage_lineage: List[Dict[str, Any]],
            prior_lineage: List[Dict[str, Any]],
            artifacts: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        # --- RunResult 契约字段 ---
        "status": status,
        "outputFile": output_file,
        # [v4.2] 血缘输出已停用：run() 返回不再携带 lineageFile/stageLineage/priorLineage 键
        # （同名形参保留以稳定调用签名，不再使用）。
        # "lineageFile": lineage_file,
        "statistics": statistics,
        "message": message,
        # --- 内部字段：app.py 负责转换为三接口返回，不直接外泄 ---
        "runId": run_id,
        "runDir": run_dir,
        "entrypoint": entrypoint,
        "rejectsFile": rejects_file,
        "manifestFile": manifest_file,
        # "stageLineage": stage_lineage,
        # "priorLineage": prior_lineage,
        "artifacts": artifacts,
    }


def _simple_failure(message: str, statistics: Dict[str, Any]) -> Dict[str, Any]:
    return _result("failed", None, "", statistics, message, None, None, MAIN_NAME,
                   None, None, [], [], [])


def _failed_result(run_dir: Path, message: str, statistics: Dict[str, Any],
                   snapshot: Dict[str, Any], run_id: str,
                   unique_id: str, stage_id: str, input_file: str, started: str,
                   logger, prior_lineage: List[Dict[str, Any]]) -> Dict[str, Any]:
    """失败结果：不返回任何成功产物。[v4.2] 血缘已停用，失败路径不再落盘血缘。

    [v4.2] 已移除 ``lineage_path`` 形参（原血缘落盘路径，v4.2 不再产出）。
    """
    log_action(logger, "失败", message)
    # [v4.2] 血缘输出已停用：失败路径不再构造/追加血缘记录。
    # record = make_lineage(
    #     OPERATOR_DIR_NAME, snapshot.get("deployment", {}).get(
    #         "operator", {}).get("version", "unknown"),
    #     unique_id, stage_id, run_id, input_file, str(Path(run_dir) / MAIN_NAME),
    #     "failed", started, now_iso(), snapshot, statistics, [])
    # stage_lineage = append_lineage(lineage_path, record)
    close_logger(logger)
    return _result("failed", None, "", statistics, message, run_id,
                   str(run_dir), MAIN_NAME, None, None, [],
                   prior_lineage, [])


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """处理单个素材包，返回 RunResult（本地路径形式）。"""
    try:
        cfg, snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        return _simple_failure("配置校验失败：%s" % type(exc).__name__,
                               {"config_error": type(exc).__name__})

    root = Path(output_root) if output_root else resolve_output_root(cfg)
    try:
        task = task_dir(root, unique_id)
    except ValueError as exc:
        return _simple_failure("uniqueId 非法：%s" % exc, {"error": "invalid_unique_id"})

    logger = None
    try:
        lock = TaskLock(root, unique_id).acquire()
    except TaskBusyError:
        return _simple_failure("任务 %s 正在被另一个写操作占用" % unique_id,
                               {"error": "task_busy"})
    except OSError as exc:
        return _simple_failure("任务锁创建失败：%s" % type(exc).__name__,
                               {"error": "lock_unavailable"})
    try:
        return _run_locked(cfg, snapshot, task, root, unique_id, str(input_file))
    finally:
        lock.release()


def _resolve_media_root(input_path: Path, task_input_dir: Path) -> Path:
    """媒体相对路径的根：优先入口同目录 manifest，其次任务 input 目录。"""
    if (input_path.parent / MANIFEST_NAME).is_file():
        return input_path.parent
    if (task_input_dir / MANIFEST_NAME).is_file():
        return task_input_dir
    return input_path.parent


def _run_locked(cfg: OperatorConfig, snapshot: Dict[str, Any], task: Path, root: Path,
                unique_id: str, input_file: str) -> Dict[str, Any]:
    operator_dir = task / OPERATOR_DIR_NAME
    operator_dir.mkdir(parents=True, exist_ok=True)
    run_id = next_run_id(operator_dir)
    run_dir = operator_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    # [v4.2] 血缘输出已停用：不再生成本地血缘快照路径（失败分支统一传占位 None）。
    # lineage_path = run_dir / LINEAGE_NAME
    logger = get_logger("%s.%s.%s" % (OPERATOR_DIR_NAME, unique_id, run_id),
                        run_dir / "run.log")
    started = now_iso()
    stage_id = cfg.stage_id
    version = snapshot["deployment"]["operator"]["version"]
    input_path = Path(input_file)
    task_input = task / "input"
    # [v4.2] 血缘已停用：不再读取历史阶段血缘归档。
    # prior_lineage = load_prior_lineage([task_input, task, run_dir])
    prior_lineage: List[Dict[str, Any]] = []

    stats: Dict[str, Any] = {
        "total_lines": 0, "valid_records": 0, "bad_lines": 0,
        "output": 0, "rejected": 0, "not_applicable": 0,
        "text_cleaned": 0, "text_unchanged": 0, "flagged": 0,
        "html_tags_removed": 0, "script_style_removed": 0,
        "control_chars_removed": 0, "invisible_chars_removed": 0,
        "whitespace_normalized": 0, "normalization_changes": 0,
        "replacement_chars": 0, "lowercased": 0, "punctuation_normalized": 0,
        "ad_patterns_removed": {}, "tokens": 0,
        "media_copied": 0, "missing_media": 0, "duplicates": 0,
        "tokenizer": None, "enabled": cfg.enabled, "requested_tokenizer": None,
        "degraded": 0, "degrade_reason": None, "lang": cfg.lang,
        "encoding": "utf-8", "conservation_ok": False,
    }

    log_action(logger, "输入", "加载输入文件：%s" % input_path.name)
    log_action(logger, "配置", "文本兜底已开启：不分词" if cfg.enabled else "文本兜底关闭，不执行、不分词")

    if not input_path.is_file():
        stats["error"] = "input_not_found"
        return _failed_result(run_dir, "输入文件不存在：%s" % input_path.name, stats,
                              snapshot, run_id, unique_id, stage_id,
                              str(input_path), started, logger, prior_lineage)
    try:
        assert_utf8(input_path)
    except UnicodeDecodeError:
        stats["error"] = "invalid_encoding"
        return _failed_result(run_dir, "输入文件不是严格 UTF-8，按 invalid_encoding 结束",
                              stats, snapshot, run_id, unique_id, stage_id,
                              str(input_path), started, logger, prior_lineage)
    except OSError as exc:
        stats["error"] = "input_unreadable"
        return _failed_result(run_dir, "输入文件读取失败：%s" % type(exc).__name__, stats,
                              snapshot, run_id, unique_id, stage_id,
                              str(input_path), started, logger, prior_lineage)

    business_cfg = dict(snapshot["business"])
    tokenizer, degraded, degrade_reason = "not_performed", False, None
    stats["tokenizer"] = tokenizer
    media_root = _resolve_media_root(input_path, task_input)
    out_path = run_dir / MAIN_NAME
    reject_path = run_dir / REJECT_NAME
    out_tmp = out_path.with_name(out_path.name + ".tmp")
    reject_tmp = reject_path.with_name(reject_path.name + ".tmp")
    copied: Dict[str, str] = {}
    seen_ids: Dict[str, int] = {}
    failure_reason: Optional[str] = None

    with JsonlWriter(out_tmp) as out_writer, JsonlWriter(reject_tmp) as reject_writer:
        try:
            for lineno, record, error in read_jsonl_stream(input_path):
                stats["total_lines"] += 1
                if error:
                    stats["bad_lines"] += 1
                    _add_reason(stats, error)
                    reject_writer.write(_reject(None, lineno, error))
                    continue
                stats["valid_records"] += 1
                sample_id = record.get("sample_id")
                if isinstance(sample_id, str) and sample_id.strip():
                    if sample_id in seen_ids:
                        stats["duplicates"] += 1
                        stats["rejected"] += 1
                        _add_reason(stats, "duplicate_sample_id")
                        reject_writer.write(_reject(
                            sample_id, lineno, "duplicate_sample_id", record=record,
                            evidence={"first_seen_line": seen_ids[sample_id]}))
                        continue
                    seen_ids[sample_id] = lineno
                try:
                    destination, payload = process_record(
                        record, lineno, business_cfg, media_root, run_dir, copied, stats)
                except tp.TokenizerUnavailableError:
                    failure_reason = "tokenizer_unavailable"
                    break
                if destination == "output":
                    out_writer.write(payload)
                    stats["output"] += 1
                else:
                    reject_writer.write(payload)
                    stats["rejected"] += 1
                    # 拒绝原因在唯一出口统一计数，避免各分支漏计或重复计数
                    _add_reason(stats, payload["reason"])
        except UnicodeDecodeError:
            failure_reason = "invalid_encoding"
        except OSError as exc:
            failure_reason = "io_error:%s" % type(exc).__name__

    if failure_reason:
        stats["error"] = failure_reason
        # 保留已写入的部分产物便于排查，但不作为成功输出返回
        for tmp_path, final_path in ((out_tmp, out_path), (reject_tmp, reject_path)):
            if tmp_path.exists():
                os.replace(str(tmp_path), str(final_path))
        return _failed_result(run_dir, "处理中断：%s" % failure_reason, stats,
                              snapshot, run_id, unique_id, stage_id, str(input_path),
                              started, logger, prior_lineage)

    os.replace(str(out_tmp), str(out_path))
    os.replace(str(reject_tmp), str(reject_path))

    stats["conservation_ok"] = (
        stats["output"] + stats["rejected"] == stats["valid_records"]
        and stats["total_lines"] == stats["valid_records"] + stats["bad_lines"])
    if not stats["conservation_ok"]:
        stats["error"] = "conservation_broken"
        return _failed_result(run_dir, "统计不守恒：output+rejected != valid_records",
                              stats, snapshot, run_id, unique_id, stage_id,
                              str(input_path), started, logger, prior_lineage)

    artifacts = [{"path": MAIN_NAME, "role": "main"},
                 {"path": REJECT_NAME, "role": "sidecar"}]
    artifacts += [{"path": relative, "role": "media"} for relative in sorted(copied)]
    manifest = build_manifest(MAIN_NAME, artifacts)
    write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    if stats["total_lines"] == 0:
        status = "failed"
        message = "empty_input：输入文件没有可解析记录"
    elif stats["output"] == 0:
        status = "partial"
        message = "全部 %d 条记录被拒绝，详见 %s" % (stats["valid_records"], REJECT_NAME)
    elif stats["degraded"]:
        status = "degraded"
        message = "分词后端降级为 %s（%s），产出 %d 条" % (
            tokenizer, degrade_reason, stats["output"])
    elif stats["rejected"] or stats["bad_lines"]:
        status = "partial"
        message = "产出 %d 条，拒绝 %d 条，坏行 %d 行" % (
            stats["output"], stats["rejected"], stats["bad_lines"])
    else:
        status = "success"
        message = "产出 %d 条，全部成功" % stats["output"]

    log_action(logger, "统计", "总行=%d 有效=%d 产出=%d 拒绝=%d 坏行=%d 不适用=%d 分词=%s" % (
        stats["total_lines"], stats["valid_records"], stats["output"], stats["rejected"],
        stats["bad_lines"], stats["not_applicable"], tokenizer))
    log_action(logger, "输出", "主清单=%s 异常清单=%s 媒体=%d" % (
        MAIN_NAME, REJECT_NAME, stats["media_copied"]))
    if status == "failed":
        log_action(logger, "失败", message)

    # [v4.2] 血缘/阶段归档已停用：不再构造血缘记录、追加血缘快照或写 stage_context.json。
    # record = make_lineage(OPERATOR_DIR_NAME, version, unique_id, stage_id, run_id,
    #                       str(input_path), str(out_path), status, started, now_iso(),
    #                       snapshot, stats, artifacts)
    # stage_lineage = append_lineage(lineage_path, record)
    # stage_context = build_stage_context(stage_id, unique_id, run_id, MAIN_NAME,
    #                                     stage_lineage, prior_lineage)
    # atomic_write_json(run_dir / STAGE_CONTEXT_NAME, stage_context)
    close_logger(logger)

    # 失败时不返回任何成功产物（文件保留在运行目录，仅用于排查）
    return _result(status, None if status == "failed" else str(out_path),
                   "", stats, message, run_id, str(run_dir), MAIN_NAME,
                   str(reject_path), str(run_dir / MANIFEST_NAME), [],
                   prior_lineage, artifacts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inference.py",
        description="算子 3 文本清洗标准化与分词：清洗 JSONL 文本并输出分词与偏移")
    parser.add_argument("--input", required=True,
                        help="输入 JSONL 路径，例如 <task>/input/02_screened.jsonl")
    parser.add_argument("--unique-id", required=True, help="任务 uniqueId")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 configs/config.yaml")
    parser.add_argument("--output-root", "--output-dir", dest="output_root", default=None,
                        help="输出根目录，默认配置 io.output_root（--output-dir 为向后兼容别名）")
    parser.add_argument("--override", action="append", default=[],
                        help="业务参数覆盖 key=value（可重复，值按 YAML 标量解析）")
    return parser


def parse_overrides(items: Iterable[str]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--override 需要 key=value 形式：%s" % item)
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--override 的 key 不能为空")
        overrides[key] = yaml.safe_load(raw)
    return overrides


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        overrides = parse_overrides(args.override)
    except ValueError as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False))
        return 2
    result = run(args.input, args.unique_id, args.config, args.output_root, overrides)
    # [v4.2] 血缘输出已停用：CLI 摘要不再包含 lineageFile
    summary = {key: result.get(key) for key in
               ("status", "outputFile", "statistics", "message")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())

