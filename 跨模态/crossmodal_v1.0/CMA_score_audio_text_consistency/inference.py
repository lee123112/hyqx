"""算子 13「评分音文语义一致性」业务入口（CLI + 可复用 ``run``）。

固定口径（表1 #13 v2）：
- 默认关闭；关闭时不加载模型、不写音文评分文件、不隔离，记录原样透传；
- 开启后复用上游 #10 Whisper-base 转写，只计算规范化字符级 CER/编辑距离比例；
  比较前默认先做繁体→简体字符级归一化（`traditional_to_simplified`，可配关闭）；
- CER 不超过任务阈值才通过并交 #19，超阈值进入样本隔离台账 #21；
- Wav2Vec2/BERT 仅允许显式注入做实验诊断，绝不参与生产判定。

本模块只写 ``OUTPUT_DIR/<uniqueId>/``；正式接口见 ``app.py``。
"""
from __future__ import annotations

import argparse
import json
import os
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

from typing import Any, Dict, List, Optional

import yaml

from utils import audio_text
from utils import calibration
from utils import io as io_utils
# [v4.2] 血缘输出已停用：lineage_mod 仅保留 now_iso 时间工具的调用，
# make_lineage / load_prior_lineage / build_stage_context / to_stage_lineage 的调用点已全部注释，代码保留存档。
from utils import lineage as lineage_mod
from utils import logger as logger_mod
from utils import pinyin as pinyin_mod
from utils import scoring
from utils import transport
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig

OPERATOR = audio_text.OPERATOR
OPERATOR_VERSION = audio_text.OPERATOR_VERSION
FORMAL_OPERATOR_NUMBER = audio_text.FORMAL_OPERATOR_NUMBER
STAGE_ID = audio_text.STAGE_ID

MAX_UPLOAD_BYTES = 67108864
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864
MAX_FILES = 1000

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
INPUT_DIR_NAME = "input"
MEDIA_DIR_NAME = "media"


class InputError(ValueError):
    """输入素材不可用。"""


#: 依赖/权重缺失（对外统一为 400 + ModelUnavailable）
DependencyError = scoring.DependencyError


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def default_config_path() -> Path:
    override = os.environ.get("OPERATOR_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config(config_path=None, overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """读取 YAML 配置并合并请求覆盖，做结构化与定义域校验。"""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError("配置文件无法解析：%s" % type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")
    payload = dict(raw)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ConfigError("config 必须是对象，不能是数组或标量")
        try:
            provided = BusinessOverrides.model_validate(overrides).provided()
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - pydantic 校验错误统一为 ConfigError
            raise ConfigError("config 非法：%s" % _validation_message(exc)) from exc
        payload.update(provided)
    try:
        return OperatorConfig.model_validate(payload)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConfigError("配置非法：%s" % _validation_message(exc)) from exc


def _validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for item in errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            parts.append("%s: %s" % (location or "<config>", item.get("msg", "非法")))
        if parts:
            return "；".join(parts)
    return str(exc)


def config_snapshot(config: OperatorConfig,
                    overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """血缘 config 快照：业务口径 + 部署身份 + 实际覆盖键。"""
    snapshot: Dict[str, Any] = {
        "business": config.business_dict(),
        "deployment": config.deployment_dict(),
        "overridden_keys": [],
    }
    if overrides:
        provided = BusinessOverrides.model_validate(overrides).provided()
        snapshot["overridden_keys"] = sorted(provided.keys())
        snapshot["overrides"] = provided
    return snapshot


# --------------------------------------------------------------------------- #
# 路径与素材
# --------------------------------------------------------------------------- #

def default_output_root() -> Path:
    override = os.environ.get("OUTPUT_DIR")
    return Path(override) if override else BASE_DIR / "output"


def resolve_input(input_file: Optional[str], task_root: Path, *,
                  allow_external_input: bool) -> Path:
    """解析入口 JSONL：阶段内后续算子传上一节点临时 outputFile，阶段首从任务输入目录取。"""
    if input_file:
        if allow_external_input:
            candidate = io_utils.resolved_absolute(input_file)
        else:
            candidate = io_utils.resolve_in_task(task_root, input_file)
        if not candidate.is_file():
            raise InputError("输入文件不存在：%s" % candidate.name)
        return candidate
    input_dir = task_root / INPUT_DIR_NAME
    if not input_dir.is_dir():
        raise InputError("任务目录缺少输入素材（%s），且请求未提供 inputFile" % INPUT_DIR_NAME)
    path, _name = transport.resolve_entrypoint(input_dir)
    return path


def _media_roots(input_file: Path, task_root: Path) -> List[Path]:
    """媒体相对路径的解析根：输入文件所在目录、其父目录、任务输入目录。"""
    roots: List[Path] = []
    for candidate in (input_file.parent, input_file.parent.parent, task_root / INPUT_DIR_NAME,
                      task_root):
        resolved = io_utils.resolved_absolute(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _carry_media(run_dir: Path, audio_path: Path, relative: str) -> bool:
    """把被引用的音频按原相对路径复制到本次运行目录（可搬运交付）。"""
    try:
        normalized = io_utils.normalize_zip_member_name(relative.replace("\\", "/"))
    except ValueError:
        return False
    # 保持素材包内的相对路径（如 media/seg_01.wav），便于下游按原路径读取
    target = run_dir / normalized
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(audio_path, target)
    except OSError:
        return False
    return True


def _carry_record_media(run_dir: Path, media_roots: List[Path], spec: Dict[str, Any],
                        stats: Dict[str, Any]) -> None:
    """尽力把被引用的音频搬到本次运行目录；不可用时只计数，不影响记录判定。"""
    audio_ref = spec.get("audio") or {}
    relative = audio_ref.get("path")
    if not relative:
        return
    try:
        resolved = audio_text.resolve_media_file(media_roots, relative)
    except io_utils.UnsupportedMediaPath:
        stats["media_missing"] += 1
        return
    if resolved is None:
        stats["media_missing"] += 1
        return
    if _carry_media(run_dir, resolved, relative):
        stats["media_carried"] += 1
    else:
        stats["media_missing"] += 1


# --------------------------------------------------------------------------- #
# 模型可用性
# --------------------------------------------------------------------------- #

def _resolve_models(config: OperatorConfig, injected_scorer) -> Dict[str, Any]:
    """旧版模型探测入口，保留给离线兼容调用。

    生产 ``run`` 不调用此函数；#13 v2 只复用上游 Whisper 转写并执行规则评分。
    """
    if injected_scorer is not None:
        return {
            "scorer": injected_scorer,
            "available": True,
            "source": "injected",
            "report": {"available": True, "source": "injected", "missing": []},
        }
    report = scoring.probe_models(config.model_root, config.audio_model_dir,
                                  config.text_model_dir)
    if report["available"]:
        try:
            scorer = scoring.load_scorer(config)
        except scoring.DependencyError as exc:
            report = dict(report)
            report["available"] = False
            report["missing"] = list(report["missing"]) + ["load_failed:%s" % exc]
            scorer = None
        else:
            return {"scorer": scorer, "available": True, "source": "local_weights",
                    "report": report}
    if not config.allow_degraded:
        raise scoring.DependencyError(
            "音文路线已开启，但本地模型不可用：%s" % "；".join(report["missing"]))
    return {"scorer": None, "available": False, "source": "degraded", "report": report}


def _resolve_auxiliary_model(config: OperatorConfig, injected_scorer) -> Dict[str, Any]:
    """Whisper-base is produced upstream by #10 and is never reloaded here."""
    source = "disabled" if not config.enabled else "upstream"
    reason = "route_disabled" if not config.enabled else "reuse_upstream_whisper_transcript"
    return {"scorer": None, "available": False, "source": source,
            "report": {"available": False, "source": source, "missing": [],
                       "reason": reason}}


def _resolve_mapping(config: OperatorConfig) -> Dict[str, Any]:
    if not config.mapping_enabled:
        return {"mapping": None, "available": False, "source": "disabled", "report": {"enabled": False}}
    if not config.mapping_path:
        return {"mapping": None, "available": False, "source": "missing", "report": {"enabled": True, "reason": "mapping_path_empty"}}
    try:
        mapping = calibration.CosineScoreMapping.from_json(config.mapping_path)
    except Exception as exc:  # noqa: BLE001 - never turn an invalid map into a gate
        return {"mapping": None, "available": False, "source": "invalid",
                "report": {"enabled": True, "reason": "%s: %s" % (type(exc).__name__, exc)}}
    return {"mapping": mapping, "available": True, "source": "local_file",
            "report": {"enabled": True, "mapping": mapping.snapshot()}}


def _model_snapshot(scorer, source: str, config: OperatorConfig) -> Optional[Dict[str, Any]]:
    """血缘里的模型身份；未真正调用模型时显式为 null，绝不冒充 model。"""
    if scorer is None:
        return None
    return {
        "audio_model": getattr(scorer, "audio_model_id", config.audio_model),
        "text_model": getattr(scorer, "text_model_id", config.text_model),
        "audio_model_version": getattr(scorer, "audio_model_version", None),
        "text_model_version": getattr(scorer, "text_model_version", None),
        "device": getattr(scorer, "device", config.device),
        "source": source,
        "weights_local_only": True,
    }


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def _initial_statistics(config: OperatorConfig, entrypoint: str) -> Dict[str, Any]:
    return {
        "enabled": bool(config.enabled),
        "audio_text_file_written": bool(config.enabled),
        "entrypoint": entrypoint,
        "input_records": 0,
        "blank_lines": 0,
        "valid_records": 0,
        "bad_lines": 0,
        "origin_quarantined": 0,
        "sample_isolated": 0,
        "pending_records": 0,
        "passed_records": 0,
        "skipped_records": 0,
        "other_modality_records": 0,
        "scored_records": 0,
        "edit_distance_exceeded": 0,
        "cosine_below_threshold": 0,
        "missing_transcript": 0,
        "missing_description": 0,
        "no_audio_text_pair": 0,
        # 非中文音文对：不适用，不隔离（见 language_applicable）
        "language_not_applicable": 0,
        # 转写不可靠（解码循环 / 语速超限）：不适用，不隔离（见 transcript_reliability）
        "transcript_unreliable": 0,
        # 同音容错开启但拼音表不可用：依赖缺失，不适用、不隔离（见 pinyin_table_unavailable）
        "pinyin_table_unavailable": 0,
        # 字符 CER 超阈值、但同音容错口径（同音字替换不计距离）在阈值内：计入通过
        "homophone_tolerated": 0,
        "score_error_records": 0,
        "degraded_records": 0,
        "media_carried": 0,
        "media_missing": 0,
        "models_available": False,
        "model_source": None,
        "audio_model": None,
        "text_model": None,
        "audio_model_version": None,
        "text_model_version": None,
        "cosine_threshold": None,
        "edit_distance_ratio_max": config.edit_distance_ratio_max,
        # 繁→简归一化开关（默认开启）：仅影响文本比较口径，不改阈值语义
        "traditional_to_simplified": bool(config.traditional_to_simplified),
        "degraded": False,
        "encoding_error": False,
        "file_read_incomplete": False,
        "empty_input": False,
        "conservation_ok": False,
        "auxiliary_enabled": False,
        "auxiliary_available": False,
        "auxiliary_source": None,
    }


def _conservation(stats: Dict[str, Any]) -> bool:
    first = stats["input_records"] == stats["valid_records"] + stats["bad_lines"]
    second = stats["valid_records"] == (stats["origin_quarantined"] + stats["sample_isolated"]
                                       + stats["pending_records"] + stats["passed_records"]
                                       + stats["skipped_records"])
    third = stats["sample_isolated"] == stats["edit_distance_exceeded"]
    fourth = stats["scored_records"] == (stats["passed_records"]
                                        + stats["edit_distance_exceeded"])
    fifth = stats["pending_records"] == (stats["missing_transcript"]
                                        + stats["missing_description"]
                                        + stats["no_audio_text_pair"]
                                         + stats["score_error_records"]
                                         + stats["degraded_records"])
    return bool(first and second and third and fourth and fifth)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: str = "", config_path=None,
        output_root=None, overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = False, scorer=None,
        auxiliary_scorer=None) -> Dict[str, Any]:
    """执行一次音文评分；产物只写入 ``OUTPUT_DIR/<uniqueId>/``。"""
    config = load_config(config_path, overrides)
    # 同音容错开启时拼音表是硬依赖：缺失会让同音误写按严格字符 CER 判「语义不通」。
    # 这里在写任何产物前快速失败，避免整批正样本被静默误隔离。
    if config.enabled and getattr(config, "homophone_tolerance", True):
        if not pinyin_mod.table_available():
            raise ConfigError(
                "同音容错（homophone_tolerance）已开启，但拼音表不可用："
                "请补齐 utils/data/PinyinTable.txt 或 $MODEL_ROOT/pinyin/PinyinTable.txt，"
                "或显式设置 homophone_tolerance: false 后重跑")
    root = io_utils.resolve_root(output_root if output_root is not None else default_output_root())
    task_root = io_utils.task_dir(root, unique_id)
    source = resolve_input(input_file, task_root, allow_external_input=allow_external_input)

    if config.enabled:
        # v2 生产判定复用上游 Whisper-base 转写，只做文本一致性规则。
        # Wav2Vec2/BERT 不再作为本算子依赖；显式传入 scorer 仅用于实验诊断。
        models = {
            "scorer": scorer,
            "available": False,
            "source": "diagnostic_injected" if scorer is not None else "rule_only",
            "report": {
                "available": False,
                "source": "diagnostic_injected" if scorer is not None else "rule_only",
                "missing": [],
                "reason": "production_uses_upstream_whisper_transcript",
            },
        }
    else:
        # 关闭路线：不加载模型、不需要权重，也不因依赖缺失失败
        models = {"scorer": None, "available": False, "source": "disabled",
                  "report": {"available": False, "source": "disabled", "missing": [],
                             "reason": "route_disabled"}}
    active_scorer = models["scorer"]
    # Whisper-base is an upstream #10 artifact; this operator never reloads it.
    auxiliary_models = {"scorer": None, "available": False, "source": "upstream",
                        "report": {"available": False, "source": "upstream", "missing": [],
                                   "reason": "reuse_upstream_whisper_transcript"}}
    active_auxiliary_scorer = None
    active_mapping = None

    operator_dir = task_root / OPERATOR
    run_id = io_utils.next_run_id(operator_dir)
    run_dir = operator_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    log = logger_mod.get_logger("operator13.%s.%s" % (unique_id, run_id),
                                run_dir / ("%s.log" % run_id))
    logger_mod.log_action(log, "输入", "uniqueId=%s 输入=%s" % (unique_id, source.name))

    entrypoint = audio_text.MAIN_NAME_ENABLED if config.enabled else audio_text.MAIN_NAME_DISABLED
    stats = _initial_statistics(config, entrypoint)
    stats["models_available"] = bool(models["available"])
    stats["model_source"] = models["source"] if config.enabled else None
    # A rule-only route is complete, not a dependency degradation.
    stats["degraded"] = False
    stats["auxiliary_available"] = bool(auxiliary_models["available"])
    stats["auxiliary_source"] = auxiliary_models["source"]
    if models["available"]:
        snapshot = _model_snapshot(active_scorer, models["source"], config)
        stats["audio_model"] = snapshot.get("audio_model")
        stats["text_model"] = snapshot.get("text_model")
        stats["audio_model_version"] = snapshot.get("audio_model_version")
        stats["text_model_version"] = snapshot.get("text_model_version")

    started_at = lineage_mod.now_iso()
    event_time = started_at
    media_roots = _media_roots(source, task_root)

    main_partial = run_dir / (entrypoint + ".partial")
    rejects_partial = run_dir / (audio_text.REJECTS_NAME + ".partial")
    quarantine_partial = run_dir / (audio_text.SAMPLE_QUARANTINE_NAME + ".partial")
    pending_partial = run_dir / (audio_text.PENDING_NAME + ".partial")
    partials = [main_partial, rejects_partial, quarantine_partial, pending_partial]

    main_writer = io_utils.JsonlWriter(main_partial)
    rejects_writer = io_utils.JsonlWriter(rejects_partial)
    quarantine_writer = io_utils.JsonlWriter(quarantine_partial)
    pending_writer = io_utils.JsonlWriter(pending_partial)
    writers = [main_writer, rejects_writer, quarantine_writer, pending_writer]
    flow_writer = None
    if config.enabled:
        flow_partial = run_dir / (audio_text.FLOW_NAME + ".partial")
        partials.append(flow_partial)
        flow_writer = io_utils.JsonlWriter(flow_partial)
        writers.append(flow_writer)

    status = "success"
    message = ""
    try:
        line_number = 0
        try:
            for raw_line in io_utils.iter_decoded_lines(source):
                if not raw_line.strip():
                    stats["blank_lines"] += 1
                    continue
                line_number += 1
                stats["input_records"] += 1
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    rejects_writer.write(audio_text.make_ledger_entry(
                        line=line_number, reason=audio_text.REASON_JSON_PARSE_ERROR,
                        label=audio_text.LABEL_ORIGIN_UNPARSABLE, action=audio_text.ACTION_RECORD,
                        destination=audio_text.DEST_ORIGIN, event_time=event_time))
                    continue
                if not isinstance(record, dict):
                    stats["bad_lines"] += 1
                    rejects_writer.write(audio_text.make_ledger_entry(
                        line=line_number, reason=audio_text.REASON_RECORD_NOT_OBJECT,
                        label=audio_text.LABEL_ORIGIN_UNPARSABLE, action=audio_text.ACTION_RECORD,
                        destination=audio_text.DEST_ORIGIN, event_time=event_time))
                    continue

                spec = audio_text.resolve_pair(record)
                spec["record"] = record
                _carry_record_media(run_dir, media_roots, spec, stats)

                # 结构性缺字段：即使路线关闭也属原始文件问题，进原始隔离区
                if not spec.get("identifier"):
                    stats["valid_records"] += 1
                    stats["origin_quarantined"] += 1
                    rejects_writer.write(audio_text.make_ledger_entry(
                        line=line_number, reason=audio_text.REASON_MISSING_FIELD,
                        label=audio_text.LABEL_ORIGIN_MISSING_FIELD,
                        action=audio_text.ACTION_RECORD, destination=audio_text.DEST_ORIGIN,
                        event_time=event_time))
                    continue

                if not config.enabled:
                    # 关闭路线：不加载模型、不写音文评分文件、不隔离（样本），仅透传
                    record.setdefault("processing", {})["13"] = audio_text.skipped_node()
                    main_writer.write(record)
                    stats["valid_records"] += 1
                    stats["skipped_records"] += 1
                    continue

                stats["valid_records"] += 1
                outcome = audio_text.evaluate_record(
                    spec, line_number, media_roots=media_roots, config=config,
                    scorer=active_scorer, auxiliary_scorer=active_auxiliary_scorer,
                    mapping=active_mapping,
                    event_time=event_time)
                action = outcome["action"]
                if action in ("pass", "skip", "pending", "other_modality"):
                    flow_writer.write(outcome["record"])
                if action == "pass":
                    main_writer.write(outcome["record"])
                    stats["passed_records"] += 1
                    stats["scored_records"] += 1
                    if outcome["reason"] == audio_text.REASON_PASSED_HOMOPHONE_TOLERANT:
                        stats["homophone_tolerated"] += 1
                elif action == "isolate":
                    quarantine_writer.write(outcome["entry"])
                    stats["sample_isolated"] += 1
                    if outcome["reason"] == audio_text.REASON_EDIT_EXCEEDED:
                        stats["edit_distance_exceeded"] += 1
                    # Every non-pending rule evaluation is a scored record,
                    # including a CER rejection. There is no cosine gate in v2.
                    stats["scored_records"] += 1
                elif action == "skip":
                    # 不适用（不是失败）：非中文音文对 / 转写不可靠。
                    # 记录留在主产物，交下游继续判定。
                    main_writer.write(outcome["record"])
                    stats["skipped_records"] += 1
                    if outcome["reason"] == audio_text.REASON_TRANSCRIPT_UNRELIABLE:
                        stats["transcript_unreliable"] += 1
                    elif outcome["reason"] == audio_text.REASON_PINYIN_TABLE_UNAVAILABLE:
                        stats["pinyin_table_unavailable"] += 1
                    else:
                        stats["language_not_applicable"] += 1
                elif action == "other_modality":
                    stats["skipped_records"] += 1
                    stats["other_modality_records"] += 1
                elif action == "pending":
                    pending_writer.write(outcome["entry"])
                    stats["pending_records"] += 1
                    reason = outcome["reason"]
                    if reason == audio_text.REASON_MISSING_TRANSCRIPT:
                        stats["missing_transcript"] += 1
                    elif reason == audio_text.REASON_MISSING_DESCRIPTION:
                        stats["missing_description"] += 1
                    elif reason == audio_text.REASON_NO_AUDIO:
                        stats["no_audio_text_pair"] += 1
                    elif reason == audio_text.REASON_MODEL_UNAVAILABLE:
                        stats["degraded_records"] += 1
                    else:
                        stats["score_error_records"] += 1
                else:  # origin：原始文件问题
                    rejects_writer.write(outcome["entry"])
                    stats["origin_quarantined"] += 1
        except UnicodeDecodeError:
            # 文件不是严格 UTF-8：整份文件不可继续读取，显式记账并停止，不静默丢弃
            stats["input_records"] += 1
            stats["bad_lines"] += 1
            stats["encoding_error"] = True
            stats["file_read_incomplete"] = True
            rejects_writer.write(audio_text.make_ledger_entry(
                line=line_number + 1, reason=audio_text.REASON_ENCODING_ERROR,
                label=audio_text.LABEL_ORIGIN_ENCODING, action=audio_text.ACTION_RECORD,
                destination=audio_text.DEST_ORIGIN, event_time=event_time))
    except BaseException:
        for writer in writers:
            writer.close()
        io_utils.cleanup_files(*partials)
        logger_mod.close_logger(log)
        raise
    finally:
        for writer in writers:
            writer.close()

    stats["empty_input"] = stats["input_records"] == 0 and stats["blank_lines"] == 0

    # 原子提交：先写好 .partial 再替换，避免下游读到半截文件
    main_path = run_dir / entrypoint
    rejects_path = run_dir / audio_text.REJECTS_NAME
    quarantine_path = run_dir / audio_text.SAMPLE_QUARANTINE_NAME
    pending_path = run_dir / audio_text.PENDING_NAME
    try:
        for partial, final in ((main_partial, main_path), (rejects_partial, rejects_path),
                               (quarantine_partial, quarantine_path),
                               (pending_partial, pending_path)):
            os.replace(str(partial), str(final))
        if flow_writer is not None:
            os.replace(flow_partial, run_dir / audio_text.FLOW_NAME)
    except OSError as exc:
        io_utils.cleanup_files(*partials)
        logger_mod.log_action(log, "警告", "产物提交失败：%s" % type(exc).__name__)
        raise

    if stats["encoding_error"] and stats["valid_records"] == 0:
        status, message = "failed", "输入文件不是严格 UTF-8，无法读取任何记录"
    elif stats["degraded"]:
        status = "degraded"
        message = ("音文路线已开启但本地模型不可用，已显式降级：%d 条记录进入待处理台账，未产出任何分数"
                   % stats["degraded_records"])
    elif stats["bad_lines"] or stats["origin_quarantined"]:
        status = "partial"
        message = "部分记录进入异常/隔离台账"
    else:
        status = "success"
        if not config.enabled:
            message = "音文路线未启用：样本原样透传，未写音文评分文件、未隔离"
        else:
            message = ("音文评分完成：通过 %d 条（其中同音容错 %d 条），不通过 %d 条，"
                       "语言不适用 %d 条，转写不可靠 %d 条，同音容错表缺失 %d 条" % (
                           stats["passed_records"], stats["homophone_tolerated"],
                           stats["sample_isolated"],
                           stats["language_not_applicable"], stats["transcript_unreliable"],
                           stats["pinyin_table_unavailable"]))

    flow_entrypoint = audio_text.FLOW_NAME if config.enabled else entrypoint
    stats["forwarded_records"] = (flow_writer or main_writer).count
    stats["conservation_ok"] = (_conservation(stats) and
                                stats["forwarded_records"] == stats["passed_records"]
                                + stats["skipped_records"] + stats["pending_records"])

    artifacts = [
        {"path": entrypoint, "role": "main"},
        {"path": audio_text.REJECTS_NAME, "role": "review"},
        {"path": audio_text.SAMPLE_QUARANTINE_NAME, "role": "review"},
        {"path": audio_text.PENDING_NAME, "role": "review"},
    ]
    if config.enabled:
        artifacts.append({"path": flow_entrypoint, "role": "pipeline"})
    manifest = transport.build_manifest(entrypoint, artifacts)
    manifest["business_entrypoint"] = entrypoint
    manifest["pipeline_entrypoint"] = flow_entrypoint
    transport.write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    media_paths: List[str] = []
    media_dir = run_dir / MEDIA_DIR_NAME
    if media_dir.is_dir():
        for path in sorted(media_dir.rglob("*")):
            if path.is_file():
                media_paths.append(str(path.relative_to(run_dir)).replace("\\", "/"))
    artifact_paths = [artifact["path"] for artifact in artifacts] + [transport.MANIFEST_NAME] + media_paths

    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录、不落盘 lineage.json / stage_context.json；
    # 代码保留存档备查。
    # snapshot = config_snapshot(config, overrides)
    # finished_at = lineage_mod.now_iso()
    # record = lineage_mod.make_lineage(
    #     operator=OPERATOR, version=config.operator.version, unique_id=unique_id,
    #     stage_id=config.stage_id, run_id=run_id, input_file=str(source),
    #     output_file=str(main_path), status=status, started_at=started_at,
    #     finished_at=finished_at, config=snapshot, statistics=stats, artifacts=artifacts,
    #     formal_operator_number=FORMAL_OPERATOR_NUMBER,
    #     # Production uses the upstream Whisper transcript and has no local
    #     # scorer model. Experimental injected diagnostics stay on the record
    #     # node and are intentionally absent from the production model field.
    #     model=None)
    # lineage_path = run_dir / lineage_mod.LINEAGE_NAME
    # io_utils.atomic_write_json(lineage_path, [record])
    #
    # stage_lineage = [record]
    # prior_lineage = lineage_mod.load_prior_lineage(media_roots)
    # stage_context = lineage_mod.build_stage_context(
    #     stage_id=config.stage_id, unique_id=unique_id, run_id=run_id, entrypoint=entrypoint,
    #     stage_lineage=stage_lineage, prior_lineage=prior_lineage)
    # io_utils.atomic_write_json(run_dir / lineage_mod.STAGE_CONTEXT_NAME, stage_context)

    logger_mod.log_action(log, "输出", "入口=%s 通过=%d 隔离=%d 待处理=%d 原始隔离=%d 坏行=%d"
                          % (entrypoint, stats["passed_records"], stats["sample_isolated"],
                             stats["pending_records"], stats["origin_quarantined"],
                             stats["bad_lines"]))
    logger_mod.log_action(log, "完成", "状态=%s 统计守恒=%s" % (status, stats["conservation_ok"]))
    logger_mod.close_logger(log)

    return {
        "status": status,
        "message": message,
        "outputFile": str(main_path),
        "rejectsFile": str(rejects_path),
        "sampleQuarantineFile": str(quarantine_path),
        "pendingFile": str(pending_path),
        # [v4.2] 血缘停用：返回值不再携带 "lineageFile" / "stageLineage" / "priorLineage" 键。
        "manifestPath": str(run_dir / transport.MANIFEST_NAME),
        "statistics": stats,
        "runId": run_id,
        "runDir": str(run_dir),
        "entrypoint": entrypoint,
        "flowEntrypoint": flow_entrypoint,
        "artifacts": artifacts,
        "artifactPaths": artifact_paths,
        "mediaPaths": media_paths,
        "models": models["report"],
    }


# --------------------------------------------------------------------------- #
# 响应形态
# --------------------------------------------------------------------------- #

# [v4.2] 血缘/阶段归档输出已停用：以下两个响应助手依赖 run() 的血缘键，已无调用点，
# 代码保留存档（注释），不再维护。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """中间态：``data`` 只承载当前节点血缘对象（含临时 outputFile）。"""
#     import copy
#
#     return copy.deepcopy(result["stageLineage"][-1])
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末：当前阶段完整血缘数组（已剔除临时 outputFile）。"""
#     return lineage_mod.to_stage_lineage(result["stageLineage"])


def stage_file_object(result: Dict[str, Any],
                      max_response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """阶段末：``crossmodal-files-v1`` 文件内容对象。"""
    limit = MAX_RESPONSE_BYTES if max_response_bytes is None else int(max_response_bytes)
    return transport.atomic_pack_file_object(
        result["runDir"], result["entrypoint"], result["artifactPaths"],
        max_response_bytes=limit)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 13 评分音文语义一致性（表1 #13）：真实音文余弦 + 编辑距离判定")
    parser.add_argument("--input", default=None, help="输入 JSONL 完整记录清单（阶段内可省略）")
    parser.add_argument("--unique-id", dest="unique_id", required=True, help="任务id_文件id")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根（默认 OUTPUT_DIR 或算子目录 output/）")
    parser.add_argument("--config", dest="config", default=None, help="YAML 配置路径")
    parser.add_argument("--override", dest="override", default=None,
                        help='业务参数覆盖 JSON，如 {"enabled": true, "edit_distance_ratio_max": 0.30}')
    parser.add_argument("--is-last", dest="is_last", action="store_true",
                        help="按阶段末形态预览 data（文件对象 + 阶段血缘数组）")
    parser.add_argument("--quiet", dest="quiet", action="store_true", help="只输出核心摘要")
    return parser


def cli_data_shape(status: str, is_last: bool) -> Dict[str, str]:
    # [v4.2] 形态二统一响应：去 lineageFile、始终返回 outputFile + 业务摘要，
    # 不再区分阶段末/非阶段末。
    if status == "failed":
        return {}
    return {"operator": "str", "status": "success", "outputFile": "file-object",
            "result": "object"}


def _summary(result: Dict[str, Any], is_last: bool, quiet: bool) -> Dict[str, Any]:
    stats = result["statistics"]
    payload: Dict[str, Any] = {
        "dataShape": cli_data_shape(result["status"], is_last),
        "entrypoint": result["entrypoint"],
        "runId": result["runId"],
        "runDir": result["runDir"],
        "statistics": {
            "input_records": stats["input_records"],
            "passed_records": stats["passed_records"],
            "sample_isolated": stats["sample_isolated"],
            "pending_records": stats["pending_records"],
            "origin_quarantined": stats["origin_quarantined"],
            "bad_lines": stats["bad_lines"],
            "scored_records": stats["scored_records"],
            "models_available": stats["models_available"],
            "degraded": stats["degraded"],
            "conservation_ok": stats["conservation_ok"],
        },
    }
    if not quiet:
        payload["outputFile"] = result["outputFile"]
        payload["ledgers"] = {
            "rejects": result["rejectsFile"],
            "sampleQuarantine": result["sampleQuarantineFile"],
            "pending": result["pendingFile"],
        }
        payload["models"] = result["models"]
    return payload


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = None
    if args.override:
        try:
            overrides = json.loads(args.override)
        except json.JSONDecodeError:
            _emit({"code": 1, "msg": "ConfigError: --override 必须是 JSON 对象", "data": {}})
            return 1
    try:
        result = run(args.input, args.unique_id, config_path=args.config,
                     output_root=args.output_root, overrides=overrides,
                     allow_external_input=True)
    except (ConfigError, InputError, scoring.DependencyError, io_utils.TaskBusyError,
            OSError, ValueError) as exc:
        _emit({"code": 1, "msg": "%s: %s" % (type(exc).__name__, exc), "data": {}})
        return 1
    code = 0 if result["status"] in ("success", "partial", "degraded") else 1
    _emit({"code": code, "msg": result["message"],
           "data": _summary(result, args.is_last, args.quiet)})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
