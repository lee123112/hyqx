"""算子 18「标注局部对局部」推理入口。

职责：
- 载入并严格校验配置（``configs/config.yaml``，支持请求级白名单覆盖）；
- 任务隔离（``OUTPUT_DIR/<uniqueId>/runs/runNNN``）、并发锁、媒体搬运；
- 流式处理 JSONL：异常行/缺字段/编码错误/业务拒绝**一律进清单**，不静默丢弃；
- 真实统计与守恒校验、运行日志、血缘与阶段上下文、运行清单；
- 为 ``app.py``（三接口）与 ``processor.py``（CLI）提供统一实现。

诚实边界：没有任何人工复核平台接入本算子，因此
``statistics.deliverable_records`` 与 ``statistics.human_verified_records`` **恒为 0**，
所有词-区域关系都是「待人工确认」的候选。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from contextlib import ExitStack
import os
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

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:                                       # pragma: no cover - 环境相关
    import yaml
except ImportError:                        # pragma: no cover
    yaml = None

from pydantic import ValidationError       # noqa: E402

from utils import backends                 # noqa: E402
from utils import io as io_utils           # noqa: E402
from utils import lineage                  # noqa: E402
# [v4.2] 血缘输出已停用：lineage 仅保留 now_iso/config_snapshot（通用工具）使用；
# read_lineage/write_lineage/write_stage_context 等血缘调用已全部注释。
from utils import phrase_region            # noqa: E402
from utils import schemas                  # noqa: E402
from utils import transport                # noqa: E402
from utils.logger import RunLogger         # noqa: E402
from utils.schemas import (ConfigError, OperatorConfig, PredictRequest,  # noqa: E402
                           to_config_error, validate_overrides)

__all__ = [
    "CLIP_NAME",
    "FORMAL_OPERATOR_NUMBER",
    "InputError",
    "LINEAGE_NAME",
    "LOG_NAME",
    "MAIN_NAME_DISABLED",
    "MAIN_NAME_ENABLED",
    "MANIFEST_NAME",
    "MEDIA_DIR_NAME",
    "ModelUnavailable",
    "OPERATOR",
    "OPERATOR_VERSION",
    "PENDING_NAME",
    "REJECTS_NAME",
    "SAMPLE_QUARANTINE_NAME",
    "STAGE_CONTEXT_NAME",
    "STAGE_ID",
    "build_media_roots",
    "build_parser",
    "carry_media",
    "clip_backend_payload",
    "default_config_path",
    "default_output_dir",
    "load_config",
    "main",
    "resolve_input",
    "run",
]

OPERATOR = schemas.OPERATOR_NAME
OPERATOR_VERSION = schemas.OPERATOR_VERSION
STAGE_ID = schemas.STAGE_ID
FORMAL_OPERATOR_NUMBER = schemas.FORMAL_OPERATOR_NUMBER
CLIP_NAME = schemas.CLIP_MODEL_NAME

MAIN_NAME_ENABLED = phrase_region.MAIN_NAME_ENABLED
MAIN_NAME_DISABLED = phrase_region.MAIN_NAME_DISABLED
REJECTS_NAME = phrase_region.REJECTS_NAME
SAMPLE_QUARANTINE_NAME = phrase_region.SAMPLE_QUARANTINE_NAME
PENDING_NAME = phrase_region.PENDING_NAME
LOG_NAME = phrase_region.LOG_NAME
LINEAGE_NAME = lineage.LINEAGE_NAME
STAGE_CONTEXT_NAME = lineage.STAGE_CONTEXT_NAME
MANIFEST_NAME = transport.MANIFEST_NAME
MEDIA_DIR_NAME = phrase_region.MEDIA_DIR_NAME

REASON_BUCKETS = {
    phrase_region.REASON_NO_BOXES: "no_boxes",
    phrase_region.REASON_BELOW_THRESHOLD: "below_threshold",
    phrase_region.REASON_MISSING_DESCRIPTION: "missing_description",
    phrase_region.REASON_NO_IMAGE_REFERENCE: "no_image_reference",
    phrase_region.REASON_MODEL_UNAVAILABLE: "model_unavailable",
    phrase_region.REASON_SCORE_ERROR: "score_error_records",
}

MAX_STORED_RECORD_CHARS = 32768
LOG_RECORD_LIMIT = 20000


class InputError(ValueError):
    """输入文件或请求参数非法。"""


class ModelUnavailable(RuntimeError):
    """CLIP 不可用且未开启显式降级。"""


# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #

def default_output_dir() -> Path:
    configured = ("" if __import__("os").environ.get("OUTPUT_DIR") is None
                  else str(__import__("os").environ.get("OUTPUT_DIR")).strip())
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "crossmodal_output_18"


def default_config_path() -> Path:
    import os

    configured = str(os.environ.get("OPERATOR_CONFIG") or "").strip()
    if configured:
        return Path(configured)
    return BASE_DIR / "configs" / "config.yaml"


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """读取并严格校验配置；未知键、越界阈值、类型错误一律失败。"""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise InputError("配置文件不存在：%s" % path)
    if yaml is None:                           # pragma: no cover
        raise InputError("缺少 PyYAML，无法解析配置文件：%s" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise InputError("配置文件不是合法 YAML：%s（%s）" % (path, exc))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InputError("配置文件顶层必须是映射：%s" % path)

    merged = dict(raw)
    merged.update(validate_overrides(overrides))
    try:
        return OperatorConfig(**merged)
    except ValidationError as exc:
        raise to_config_error(exc)


def clip_backend_payload(config: OperatorConfig) -> Dict[str, Any]:
    return {"library": config.clip.library,
            "model_name": config.clip.model_name,
            "weights": config.clip.weights,
            "device": config.clip.device,
            "batch_size": int(config.clip.batch_size)}


# --------------------------------------------------------------------------- #
# 输入与媒体
# --------------------------------------------------------------------------- #

def resolve_input(input_file: Optional[str], task_dir: Path,
                  allow_external_input: bool = True) -> Path:
    if input_file is None or not str(input_file).strip():
        raise InputError("inputFile 不能为空")
    raw = str(input_file).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        raise InputError("inputFile 不存在或不是文件：%s" % raw)
    if not allow_external_input:
        base = Path(task_dir).resolve()
        if base not in path.parents:
            raise InputError("inputFile 必须位于任务目录内：%s" % raw)
    return path


def build_media_roots(input_path: Path, task_root: Path) -> List[Path]:
    roots: List[Path] = []
    for candidate in (Path(input_path).parent, Path(task_root)):
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def carry_media(run_dir: Path, media_roots: Sequence[Path], relatives: Sequence[str],
                carried: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """把引用的媒体文件复制进 ``<run_dir>/media/``；返回 ``{引用: 运行目录相对路径}``。"""
    registry = carried if carried is not None else {}
    media_dir = Path(run_dir) / MEDIA_DIR_NAME
    for relative in relatives:
        if relative in registry:
            continue
        try:
            resolved = phrase_region.resolve_media_file(media_roots, relative)
        except io_utils.UnsupportedMediaPath:
            continue
        if resolved is None or not resolved.is_file():
            continue
        media_dir.mkdir(parents=True, exist_ok=True)
        used = set(registry.values())
        name = resolved.name
        candidate = name
        counter = 1
        while ("%s/%s" % (MEDIA_DIR_NAME, candidate)) in used:
            counter += 1
            candidate = "%d_%s" % (counter, name)
        shutil.copy2(resolved, media_dir / candidate)
        registry[relative] = "%s/%s" % (MEDIA_DIR_NAME, candidate)
    return registry


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def _empty_statistics(config: OperatorConfig) -> Dict[str, Any]:
    return {
        "route_enabled": bool(config.enabled),
        "input_records": 0,
        "valid_records": 0,
        "blank_lines": 0,
        "bad_lines": 0,
        "prefilled_records": 0,
        "pending_manual_records": 0,
        "skipped_records": 0,
        "sample_isolated": 0,
        "origin_quarantined": 0,
        "isolated_no_correspondence": 0,
        "scored_records": 0,
        "no_boxes": 0,
        "below_threshold": 0,
        "missing_description": 0,
        "no_image_reference": 0,
        "model_unavailable": 0,
        "score_error_records": 0,
        "tokens_total": 0,
        "tokens_dropped": 0,
        "phrases_total": 0,
        "phrases_used": 0,
        "token_variants": 0,
        "relations_total": 0,
        "relations_prefilled": 0,
        "relations_pending": 0,
        "boxes_received": 0,
        "boxes_used": 0,
        "boxes_invalid": 0,
        "crops_scored": 0,
        "crops_failed": 0,
        "media_carried": 0,
        "images_carried": 0,
        "pending_entries": 0,
        "rejects_entries": 0,
        "quarantine_entries": 0,
        "deliverable_records": 0,
        "human_verified_records": 0,
        "tokenizer_source": None,
        "tokenizer_backend": None,
        "models_available": False,
        "model_source": None,
        "clip_model": CLIP_NAME,
        "min_clip_score": float(config.min_clip_score),
        "tokenizer_mode": config.tokenizer,
        "max_tokens": int(config.max_tokens),
        "merge_phrases": bool(config.merge_phrases),
        "class_calibration": getattr(config, "class_calibration", "none"),
        "crop_padding_ratio": float(config.crop.padding_ratio),
        "background_suppression": config.crop.background_suppression,
        # 按检测类别统计词-框分数（只做校准依据，不改 0.25 阈值判定）
        "class_profile": {},
        "calibrated_records": 0,
        "calibrated_classes": 0,
        "degraded": False,
        "encoding_error": False,
        "file_read_incomplete": False,
        "empty_input": False,
        "conservation_ok": True,
        "conservation_failures": [],
    }


def _check_conservation(statistics: Dict[str, Any]) -> List[str]:
    failures: List[str] = []
    if statistics["input_records"] != statistics["valid_records"] + statistics["bad_lines"]:
        failures.append("input_records != valid_records + bad_lines")
    routed = (statistics["origin_quarantined"] + statistics["sample_isolated"]
              + statistics["pending_manual_records"] + statistics["prefilled_records"]
              + statistics["skipped_records"])
    if statistics["valid_records"] != routed:
        failures.append("valid_records != origin + sample_isolated + pending + prefilled + skipped")
    if statistics["sample_isolated"] != statistics["isolated_no_correspondence"]:
        failures.append("sample_isolated != isolated_no_correspondence")
    pending_detail = sum(statistics[key] for key in (
        "no_boxes", "below_threshold", "missing_description", "no_image_reference",
        "model_unavailable", "score_error_records"))
    if statistics["pending_manual_records"] != pending_detail:
        failures.append("pending_manual_records != 各原因之和")
    if statistics["scored_records"] != (statistics["prefilled_records"]
                                        + statistics["below_threshold"]):
        failures.append("scored_records != prefilled_records + below_threshold")
    if statistics["relations_total"] != (statistics["relations_prefilled"]
                                         + statistics["relations_pending"]):
        failures.append("relations_total != relations_prefilled + relations_pending")
    return failures


# --------------------------------------------------------------------------- #
# 台账辅助
# --------------------------------------------------------------------------- #

def _synthetic_node(config: OperatorConfig, *, status: str, action: str, reason: str,
                    label: str, destination: str, detail: str = "") -> Dict[str, Any]:
    return phrase_region.base_node(
        status=status, action=action, reason=reason, label=label,
        destination=destination, route_enabled=bool(config.enabled),
        min_clip_score=float(config.min_clip_score),
        tokenizer_mode=config.tokenizer, detail=detail)


def _bounded_record(record: Any) -> Any:
    try:
        payload = json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_truncated": True, "_reason": "record_not_serialisable"}
    if len(payload) <= MAX_STORED_RECORD_CHARS:
        return record
    return {"_truncated": True, "_size": len(payload),
            "_preview": payload[:MAX_STORED_RECORD_CHARS]}


def _prepare_encoder(config: OperatorConfig, injected: Any,
                     logger: RunLogger) -> Tuple[Any, Optional[Dict[str, Any]], str]:
    if not config.enabled:
        return None, None, "route_disabled"
    if injected is not None:
        identity = None
        try:
            identity = injected.model_identity()
        except Exception:                      # noqa: BLE001
            identity = None
        logger.event("encoder_injected", "使用注入的编码器（测试替身或复用实例）",
                     has_identity=identity is not None)
        return injected, identity, "injected"
    try:
        encoder, reason, info = backends.build_clip_backend(
            clip_backend_payload(config), allow_degraded=bool(config.allow_degraded))
    except backends.BackendUnavailable as exc:
        raise ModelUnavailable(str(exc))
    if encoder is None:
        logger.event("encoder_unavailable", reason or "unknown",
                     allow_degraded=bool(config.allow_degraded))
        return None, None, "unavailable"
    logger.event("encoder_ready", "复用已加载的 CLIP 实例",
                 model=config.clip.model_name, device=config.clip.device)
    return encoder, info, "shared-registry"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _next_run_id(task_dir: Path) -> str:
    runs = Path(task_dir) / "runs"
    highest = 0
    if runs.is_dir():
        for child in runs.iterdir():
            if child.is_dir() and child.name.startswith("run"):
                suffix = child.name[3:]
                if suffix.isdigit():
                    highest = max(highest, int(suffix))
    return "run%03d" % (highest + 1)


def _stage_status(statistics: Dict[str, Any]) -> Tuple[str, str]:
    if statistics["degraded"]:
        return "degraded", "模型不可用：按配置显式降级为待人工，未产生任何分数"
    if statistics["valid_records"] == 0 and (statistics["bad_lines"] or statistics["encoding_error"]):
        return "failed", "没有可处理的有效记录"
    return "success", "局部对局部处理完成（全部关系待人工确认）"


def run(*, input_file: Optional[str], unique_id: str,
        config_path: Optional[str] = None, output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True, encoder: Any = None) -> Dict[str, Any]:
    """执行一次算子运行，返回结果字典（同时落盘）。"""
    started_at = lineage.now_iso()
    unique = io_utils.validate_unique_id(unique_id)
    config = load_config(config_path, overrides)
    root = Path(output_root) if output_root else default_output_dir()

    with io_utils.TaskLock(root, unique):
        task_dir = root / unique
        task_dir.mkdir(parents=True, exist_ok=True)
        input_path = resolve_input(input_file, task_dir, allow_external_input)
        run_id = _next_run_id(task_dir)
        run_dir = task_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        log_path = run_dir / LOG_NAME
        logger = RunLogger(log_path)
        statistics = _empty_statistics(config)
        logger.event("run_start", OPERATOR, uniqueId=unique, runId=run_id,
                     inputFile=input_path.name, enabled=config.enabled,
                     min_clip_score=config.min_clip_score, tokenizer=config.tokenizer)
        logger.event("config", "配置生效", **config.business_snapshot())

        main_name = MAIN_NAME_ENABLED if config.enabled else MAIN_NAME_DISABLED
        main_path = run_dir / main_name
        rejects_path = run_dir / REJECTS_NAME
        quarantine_path = run_dir / SAMPLE_QUARANTINE_NAME
        pending_path = run_dir / PENDING_NAME

        media_roots = build_media_roots(input_path, task_dir)
        # [v4.2] 血缘输出已停用：不再继承阶段内上游血缘（代码保留注释存档）
        # prior_lineage = [item for item in lineage.read_lineage(run_dir / LINEAGE_NAME)
        #                  if item.get("operator") != OPERATOR]
        # input_dir_lineage = [item for item in lineage.read_lineage(input_path.parent / LINEAGE_NAME)
        #                      if item.get("operator") != OPERATOR]
        # if input_dir_lineage:
        #     prior_lineage = input_dir_lineage
        # if prior_lineage:
        #     logger.event("prior_lineage", "继承阶段内上游血缘",
        #                  operators=",".join(sorted({str(item.get("operator"))
        #                                             for item in prior_lineage})))
        # [v4.2] 血缘输出已停用：prior_lineage 不再构建/传递（仅保留空列表供兼容调用签名）
        # prior_lineage: List[Dict[str, Any]] = []

        try:
            encoder_obj, model_info, model_source = _prepare_encoder(config, encoder, logger)
        except ModelUnavailable as exc:
            logger.event("model_unavailable", str(exc))
            statistics["conservation_ok"] = True
            _write_failure_artifacts(
                run_dir=run_dir, config=config, unique=unique, run_id=run_id,
                input_path=input_path, started_at=started_at, message=str(exc),
                log_path=log_path)
            logger.close()
            raise

        statistics["models_available"] = encoder_obj is not None
        statistics["model_source"] = model_source

        carried: Dict[str, str] = {}
        seen_identifiers: set = set()

        with ExitStack() as stack:
            main_writer = stack.enter_context(io_utils.JsonlWriter(main_path))
            rejects_writer = stack.enter_context(io_utils.JsonlWriter(rejects_path))
            quarantine_writer = stack.enter_context(io_utils.JsonlWriter(quarantine_path))
            pending_writer = stack.enter_context(io_utils.JsonlWriter(pending_path))

            def _ledger(writer, node, spec, line, detail=None):
                entry = phrase_region.make_ledger_entry(
                    line=line, spec=spec, node=node, detail=detail,
                    detected_at=lineage.now_iso())
                writer.write(entry)
                return entry

            def _reject(line, reason, label, detail, spec=None):
                node = _synthetic_node(config, status=phrase_region.STATUS_ISOLATED,
                                       action=phrase_region.ACTION_ORIGIN, reason=reason,
                                       label=label,
                                       destination=phrase_region.DEST_ORIGIN, detail=detail)
                _ledger(rejects_writer, node, spec, line, detail)
                statistics["rejects_entries"] += 1

            try:
                for line_number, raw_line in enumerate(
                        io_utils.iter_decoded_lines(input_path, "utf-8"), start=1):
                    text = raw_line.strip()
                    if not text:
                        statistics["blank_lines"] += 1
                        continue
                    statistics["input_records"] += 1
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError as exc:
                        statistics["bad_lines"] += 1
                        _reject(line_number, phrase_region.REASON_JSON_PARSE,
                                phrase_region.LABEL_ORIGIN_UNPARSABLE,
                                "JSON 解析失败：%s" % exc.msg)
                        continue
                    if not isinstance(payload, dict):
                        statistics["bad_lines"] += 1
                        _reject(line_number, phrase_region.REASON_NOT_OBJECT,
                                phrase_region.LABEL_ORIGIN_UNPARSABLE,
                                "记录不是 JSON 对象：%s" % type(payload).__name__)
                        continue

                    statistics["valid_records"] += 1
                    spec = phrase_region.resolve_record(payload)
                    if not spec["identifier"]:
                        statistics["origin_quarantined"] += 1
                        _reject(line_number, phrase_region.REASON_MISSING_SAMPLE_ID,
                                phrase_region.LABEL_ORIGIN_MISSING_FIELD,
                                "缺少 sample_id / pair_id，无法定位样本", spec)
                        continue
                    if spec["identifier"] in seen_identifiers:
                        statistics["origin_quarantined"] += 1
                        _reject(line_number, phrase_region.REASON_DUPLICATE_IDENTIFIER,
                                phrase_region.LABEL_ORIGIN_DUPLICATE,
                                "标识重复：%s" % spec["identifier"], spec)
                        continue
                    seen_identifiers.add(spec["identifier"])

                    outcome = phrase_region.evaluate_record(
                        spec, line_number, media_roots=media_roots, config=config,
                        encoder=encoder_obj, event_time=lineage.now_iso())
                    node = outcome["node"]
                    action = outcome["action"]

                    if config.enabled and node.get("image_path"):
                        carry_media(run_dir, media_roots, [node["image_path"]], carried)

                    if action == phrase_region.ACTION_SKIP:
                        statistics["skipped_records"] += 1
                        main_writer.write(outcome["output"])
                    elif action == phrase_region.ACTION_PREFILL:
                        statistics["prefilled_records"] += 1
                        statistics["scored_records"] += 1
                        main_writer.write(outcome["output"])
                        _ledger(pending_writer, node, spec, line_number)
                        statistics["pending_entries"] += 1
                    elif action == phrase_region.ACTION_PENDING:
                        statistics["pending_manual_records"] += 1
                        main_writer.write(outcome["output"])
                        _ledger(pending_writer, node, spec, line_number)
                        statistics["pending_entries"] += 1
                        bucket = REASON_BUCKETS.get(node["reason"])
                        if bucket:
                            statistics[bucket] += 1
                        if node["reason"] == phrase_region.REASON_BELOW_THRESHOLD:
                            statistics["scored_records"] += 1
                        if node.get("degraded"):
                            statistics["degraded"] = True
                    elif action == phrase_region.ACTION_ISOLATE:
                        statistics["sample_isolated"] += 1
                        statistics["isolated_no_correspondence"] += 1
                        entry = _ledger(quarantine_writer, node, spec, line_number)
                        entry["record"] = _bounded_record(payload)
                        statistics["quarantine_entries"] += 1
                    elif action == phrase_region.ACTION_ORIGIN:
                        statistics["origin_quarantined"] += 1
                        _ledger(rejects_writer, node, spec, line_number)
                        statistics["rejects_entries"] += 1

                    statistics["tokens_total"] += int(node.get("token_count") or 0)
                    statistics["tokens_dropped"] += int(node.get("tokens_dropped") or 0)
                    statistics["phrases_total"] += int(node.get("phrases_total") or 0)
                    statistics["phrases_used"] += int(node.get("phrases_used") or 0)
                    statistics["token_variants"] += int(node.get("token_variants") or 0)
                    calibration = node.get("class_calibration") or {}
                    if int(calibration.get("classes") or 0) > 0:
                        statistics["calibrated_records"] += 1
                        statistics["calibrated_classes"] += int(calibration["classes"])
                    for class_name, profile in (node.get("class_profile") or {}).items():
                        bucket = statistics["class_profile"].setdefault(
                            str(class_name),
                            {"relations": 0, "prefilled": 0, "best_score": None})
                        bucket["relations"] += int(profile.get("relations") or 0)
                        bucket["prefilled"] += int(profile.get("prefilled") or 0)
                        score = profile.get("best_score")
                        if score is not None and (bucket["best_score"] is None
                                                  or score > bucket["best_score"]):
                            bucket["best_score"] = score
                    statistics["relations_total"] += int(node.get("relations_total") or 0)
                    statistics["relations_prefilled"] += int(node.get("relations_prefilled") or 0)
                    statistics["relations_pending"] += int(node.get("relations_pending") or 0)
                    statistics["boxes_received"] += int(node.get("boxes_received") or 0)
                    statistics["boxes_used"] += int(node.get("boxes_used") or 0)
                    statistics["boxes_invalid"] += int(node.get("boxes_invalid") or 0)
                    statistics["crops_scored"] += int(node.get("crops_scored") or 0)
                    statistics["crops_failed"] += int(node.get("crops_failed") or 0)
                    if node.get("tokenizer_source"):
                        statistics["tokenizer_source"] = node["tokenizer_source"]
                        statistics["tokenizer_backend"] = node["tokenizer_backend"]
                    if line_number <= LOG_RECORD_LIMIT:
                        logger.event("record", action,
                                     line=line_number, identifier=spec["identifier"],
                                     reason=node["reason"], status=node["status"],
                                     relations=node["relations_total"],
                                     prefilled=node["relations_prefilled"],
                                     boxesUsed=node["boxes_used"],
                                     bestScore=node["best_score"])
            except UnicodeDecodeError as exc:
                statistics["input_records"] += 1
                statistics["bad_lines"] += 1
                statistics["encoding_error"] = True
                statistics["file_read_incomplete"] = True
                logger.event("encoding_error", "输入不是严格 UTF-8，按块计入拒绝清单",
                             start=exc.start, reason=exc.reason)
                _reject(0, phrase_region.REASON_ENCODING,
                        phrase_region.LABEL_ORIGIN_ENCODING,
                        "UTF-8 解码失败：%s（位置 %s）" % (exc.reason, exc.start))

        statistics["media_carried"] = len(carried)
        statistics["images_carried"] = len(carried)
        statistics["empty_input"] = (statistics["input_records"] == 0
                                     and statistics["blank_lines"] == 0)
        statistics["deliverable_records"] = 0
        statistics["human_verified_records"] = 0
        failures = _check_conservation(statistics)
        statistics["conservation_failures"] = failures
        statistics["conservation_ok"] = not failures

        status, message = _stage_status(statistics)
        finished_at = lineage.now_iso()

        artifacts = [
            lineage.artifact_entry(main_name, "records"),
            lineage.artifact_entry(REJECTS_NAME, "rejects"),
            lineage.artifact_entry(SAMPLE_QUARANTINE_NAME, "quarantine"),
            lineage.artifact_entry(PENDING_NAME, "pending"),
            # [v4.2] 血缘文件已停用：不再登记 lineage.json / stage_context.json 产物
            lineage.artifact_entry(LOG_NAME, "log"),
        ]
        media_artifacts = [lineage.artifact_entry(relative, "media")
                           for relative in carried.values()]
        manifest = transport.build_manifest(main_name, artifacts + media_artifacts)
        transport.write_manifest(run_dir, manifest)
        publish_stage_output(run_dir)

        # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录、不再写 lineage.json /
        # stage_context.json（代码保留注释存档，不删除）。
        # lineage_record = {
        #     "operator": OPERATOR,
        #     "operator_version": OPERATOR_VERSION,
        #     "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
        #     "stageId": config.stage_id,
        #     "uniqueId": unique,
        #     "runId": run_id,
        #     "inputFile": str(input_path),
        #     "outputFile": str(main_path),
        #     "status": status,
        #     "message": message,
        #     "startedAt": started_at,
        #     "finishedAt": finished_at,
        #     "config": lineage.config_snapshot(config),
        #     "statistics": lineage.statistics_snapshot(statistics),
        #     "artifacts": manifest["artifacts"],
        #     "mediaArtifacts": media_artifacts,
        #     "runDir": str(run_dir),
        #     "entrypoint": main_name,
        #     "rejectsFile": str(rejects_path),
        #     "sampleQuarantineFile": str(quarantine_path),
        #     "pendingFile": str(pending_path),
        #     "logFile": str(log_path),
        #     "model": model_info,
        #     "clipModel": CLIP_NAME,
        #     "degraded": bool(statistics["degraded"]),
        #     "stageStart": prior_lineage == [],
        # }
        # stage_lineage = prior_lineage + [lineage_record]
        # lineage.write_lineage(run_dir / LINEAGE_NAME, stage_lineage)
        # stage_context = {
        #     "format": "crossmodal-stage-context-v1",
        #     "operator": OPERATOR,
        #     "stageId": config.stage_id,
        #     "uniqueId": unique,
        #     "runId": run_id,
        #     "entrypoint": main_name,
        #     "outputFile": str(main_path),
        #     "status": status,
        #     "priorOperators": [item.get("operator") for item in prior_lineage],
        #     "stageOperators": [item.get("operator") for item in stage_lineage],
        #     "statistics": lineage.statistics_snapshot(statistics),
        #     "createdAt": finished_at,
        # }
        # lineage.write_stage_context(run_dir / STAGE_CONTEXT_NAME, stage_context)

        logger.event("run_end", status, inputRecords=statistics["input_records"],
                     validRecords=statistics["valid_records"],
                     badLines=statistics["bad_lines"],
                     prefilledRecords=statistics["prefilled_records"],
                     pendingManualRecords=statistics["pending_manual_records"],
                     skippedRecords=statistics["skipped_records"],
                     sampleIsolated=statistics["sample_isolated"],
                     originQuarantined=statistics["origin_quarantined"],
                     relationsTotal=statistics["relations_total"],
                     phrasesTotal=statistics["phrases_total"],
                     phrasesUsed=statistics["phrases_used"],
                     deliverableRecords=0,
                     conservationOk=statistics["conservation_ok"])
        logger.close()

        pack_paths = [item["path"] for item in manifest["artifacts"]] + [MANIFEST_NAME]
        return {
            "status": status,
            "message": message,
            "operator": OPERATOR,
            "operatorVersion": OPERATOR_VERSION,
            "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
            "stageId": config.stage_id,
            "uniqueId": unique,
            "runId": run_id,
            # [v4.2] 血缘输出已停用：不再依据 prior_lineage 判阶段首，改用是否传入 inputFile
            "stageStart": not bool(input_file),
            "inputFile": str(input_path),
            "runDir": str(run_dir),
            "entrypoint": main_name,
            "outputFile": str(main_path),
            "rejectsFile": str(rejects_path),
            "sampleQuarantineFile": str(quarantine_path),
            "pendingFile": str(pending_path),
            # [v4.2] 血缘输出已停用：不再回传 lineageFile/contextPath/
            # stageLineage/priorLineage 键
            "manifestPath": str(run_dir / MANIFEST_NAME),
            "logPath": str(log_path),
            "artifacts": manifest["artifacts"],
            "mediaArtifacts": media_artifacts,
            "artifactPaths": [item["path"] for item in manifest["artifacts"]],
            "mediaPaths": [item["path"] for item in media_artifacts],
            "packPaths": pack_paths,
            "statistics": statistics,
            "models": backends.installed_libraries(),
            "model": model_info,
            "modelSource": model_source,
            "clipModel": CLIP_NAME,
            "config": lineage.config_snapshot(config),
            # [v4.2] 血缘输出已停用：结果不再携带 stageLineage/priorLineage 键（代码注释保留、暂不删除）
            "startedAt": started_at,
            "finishedAt": finished_at,
        }


def _write_failure_artifacts(*, run_dir: Path, config: OperatorConfig, unique: str,
                             run_id: str, input_path: Path, started_at: str,
                             message: str,
                             log_path: Path) -> None:
    """[v4.2] 血缘输出已停用：严格模式下模型不可用不再写 status=failed 血缘，
    仅保留 run.log（异常照常上抛）。原血缘/阶段上下文写入代码保留注释存档。"""
    # finished_at = lineage.now_iso()
    # record = {
    #     "operator": OPERATOR,
    #     "operator_version": OPERATOR_VERSION,
    #     "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
    #     "stageId": config.stage_id,
    #     "uniqueId": unique,
    #     "runId": run_id,
    #     "inputFile": str(input_path),
    #     "outputFile": None,
    #     "status": "failed",
    #     "message": message,
    #     "startedAt": started_at,
    #     "finishedAt": finished_at,
    #     "config": lineage.config_snapshot(config),
    #     "statistics": {"models_available": False, "deliverable_records": 0},
    #     "artifacts": [],
    #     "mediaArtifacts": [],
    #     "runDir": str(run_dir),
    #     "logFile": str(log_path),
    #     "clipModel": CLIP_NAME,
    #     "model": None,
    #     "degraded": False,
    #     "stageStart": not prior_lineage,
    # }
    # stage = prior_lineage + [record]
    # lineage.write_lineage(run_dir / LINEAGE_NAME, stage)
    # lineage.write_stage_context(run_dir / STAGE_CONTEXT_NAME, {
    #     "format": "crossmodal-stage-context-v1",
    #     "operator": OPERATOR,
    #     "stageId": config.stage_id,
    #     "uniqueId": unique,
    #     "runId": run_id,
    #     "status": "failed",
    #     "message": message,
    #     "priorOperators": [item.get("operator") for item in prior_lineage],
    #     "stageOperators": [item.get("operator") for item in stage],
    #     "createdAt": finished_at,
    # })
    _ = (run_dir, config, unique, run_id, input_path, started_at, message, log_path)


# --------------------------------------------------------------------------- #
# 阶段血缘形态（v4.2 已停用存档）
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：以下两个形态函数整段注释存档，不再被 app.py 调用。
#
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """[v4.2 已停用] ``isLastOperator=false``：当前节点血缘 + 临时 outputFile。"""
#     records = result.get("stageLineage") or []
#     node = dict(records[0]) if records else {}
#     node["outputFile"] = result.get("outputFile")
#     return node
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """``isLastOperator=true``：完整阶段血缘（不含中间 outputFile）。"""
#     payload: List[Dict[str, Any]] = []
#     for item in result.get("stageLineage") or []:
#         entry = dict(item)
#         entry.pop("outputFile", None)
#         payload.append(entry)
#     return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 18「标注局部对局部」命令行入口")
    parser.add_argument("--input", dest="input_file", required=True,
                        help="输入 JSONL 路径")
    parser.add_argument("--unique-id", dest="unique_id", required=True,
                        help="任务标识（输出到 OUTPUT_DIR/<uniqueId>/）")
    parser.add_argument("--config", dest="config_path", default=None,
                        help="配置文件路径，默认 configs/config.yaml")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根目录，默认取环境变量 OUTPUT_DIR")
    parser.add_argument("--allow-degraded", dest="allow_degraded", action="store_true",
                        help="CLIP 不可用时显式降级为待人工（默认严格失败）")
    parser.add_argument("--enable", dest="enable", action="store_true",
                        help="临时开启局部对局部路线（等价 overrides.enabled=true）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.enable:
        overrides["enabled"] = True
    if args.allow_degraded:
        overrides["allow_degraded"] = True

    try:
        result = run(input_file=args.input_file, unique_id=args.unique_id,
                     config_path=args.config_path, output_root=args.output_root,
                     overrides=overrides or None, allow_external_input=True)
    except (InputError, ConfigError, ValueError) as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {}}, ensure_ascii=False))
        return 2
    except ModelUnavailable as exc:
        print(json.dumps({"code": 1, "msg": "模型不可用：%s" % exc, "data": {}},
                         ensure_ascii=False))
        return 3

    print(json.dumps({"code": 0, "msg": result["message"],
                      "data": {"uniqueId": result["uniqueId"],
                               "status": result["status"],
                               "runDir": result["runDir"],
                               "outputFile": result["outputFile"],
                               "statistics": result["statistics"]}},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("success", "degraded") else 1


if __name__ == "__main__":                 # pragma: no cover
    raise SystemExit(main())
