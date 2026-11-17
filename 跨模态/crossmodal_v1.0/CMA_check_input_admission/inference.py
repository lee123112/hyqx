"""算子 10：核对输入对象准入 —— 配置加载、处理流程（run）与 CLI。

职责（DEVELOPMENT_PLAN §3、TECHNICAL_DESIGN §1–§3、表1 #10 冻结字段）：
- ``load_config``：YAML + 请求覆盖统一校验，API 与 CLI 共用；
- ``run``：流式读取 #9 输出 → 逐条准入核对（模态数/描述/授权/来源批次/文件可打开）→
  四路分流（通过 → 主清单；退回 #9 或批次登记 → 退回清单；原始文件问题 → 原始隔离台账；
  业务问题 → 样本隔离台账）→ 媒体搬运 → 原子落盘 → 写 manifest / lineage / stage_context /
  run.log → 返回内部 RunResult；
- 本算子是 S3 阶段的**中间节点**：中间态 ``predict false`` 只回当前节点血缘（含临时
  ``outputFile``）；阶段末 ``predict true`` 回 ``crossmodal-files-v1`` 文件内容对象与当前阶段
  完整血缘数组（阶段边界由工程侧通过 ``isLastOperator`` 决定，本算子自行不改变阶段边界）；
- CLI：``python inference.py --input <文件> --unique-id <id> --output-root <root>``。

本算子**无模型、无权重、无需训练、不联网**：准入核对是纯规则算法，不产生任何模型分数，
也不存在可被 mock 冒充实测的模型质量结论。表1 明确边界：不重做单模态画质评分。
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

from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils import admission
from utils import io as io_utils
from utils.admission import DependencyError
from utils.io import UnsupportedMediaPath
# [v4.2] 血缘/阶段归档输出已停用：仅保留 now_iso 时间工具
# from utils.lineage import (
#     LINEAGE_HISTORY_NAME,
#     STAGE_CONTEXT_NAME,
#     build_stage_context,
#     load_prior_lineage,
#     make_lineage,
#     now_iso,
#     to_stage_lineage,
# )
from utils.lineage import now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig
from utils.transport import (
    MANIFEST_NAME,
    atomic_pack_file_object,
    build_manifest,
    write_manifest,
)

#: 血缘/映射里的算子身份（demo ID，不是甲方最终模态编码）
OPERATOR = admission.OPERATOR
FORMAL_OPERATOR_NUMBER = admission.FORMAL_OPERATOR_NUMBER

#: 本算子编号对应的输出清单
MAIN_NAME = "10_admitted.jsonl"
RETURN_NAME = "10_return.jsonl"
ORIGIN_NAME = "10_origin_quarantine.jsonl"
SAMPLE_NAME = "10_sample_quarantine.jsonl"
OPERATOR_OUTPUT_NAMES = (MAIN_NAME, RETURN_NAME, ORIGIN_NAME, SAMPLE_NAME)

#: 上游旁路清单（review/待补/reject）命名，随包携带以便追溯，不重名覆盖
SIDECAR_PATTERN = re.compile(r"^\d{2}_.*\.jsonl$")

#: HTTP 服务端限额（demo 约定，写进部署血缘；不接受请求放宽）
MAX_UPLOAD_BYTES = 67108864
MAX_FILES = 1000
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864


class InputError(ValueError):
    """根输入不可读、找不到入口或历史血缘非法。"""


# ---------------------------------------------------------------- 配置加载
BASE_DIR = Path(__file__).resolve().parent


def default_config_path() -> Path:
    """默认配置路径：``OPERATOR_CONFIG`` 环境变量 > 本目录 ``configs/config.yaml``。"""
    env_path = os.environ.get("OPERATOR_CONFIG")
    if env_path:
        return Path(env_path)
    return BASE_DIR / "configs" / "config.yaml"


def _describe_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return "%s: %s" % (location, first.get("msg", "非法取值"))


def load_config(config_path: Optional[str] = None,
                overrides: Optional[dict] = None) -> OperatorConfig:
    """读取部署 YAML 并合并请求覆盖；结构与定义域问题抛 ``ConfigError``。"""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path.name)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
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
        except ValidationError as exc:
            raise ConfigError("config 非法——%s" % _describe_validation_error(exc)) from exc
        payload.update(provided)

    try:
        cfg = OperatorConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError("配置非法——%s" % _describe_validation_error(exc)) from exc
    return cfg


def config_snapshot(cfg: OperatorConfig, overridden: Optional[dict] = None) -> Dict[str, Any]:
    """血缘里的配置快照：业务参数 + 部署身份 + 被覆盖的键。"""
    snapshot: Dict[str, Any] = {
        "business": cfg.business_dict(),
        "deployment": cfg.deployment_dict(),
        "overridden_keys": sorted(overridden or {}),
    }
    if overridden:
        snapshot["overrides"] = dict(overridden)
    return snapshot


def business_config(cfg: OperatorConfig) -> Dict[str, Any]:
    """传给业务纯函数的业务参数（纯 dict，含算子身份用于台账）。"""
    payload = dict(cfg.business_dict())
    payload["operator_version"] = cfg.operator.version
    payload["operator"] = cfg.operator.name
    payload["stage_id"] = cfg.stage_id
    return payload


# ---------------------------------------------------------------- 输入定位与阶段血缘
def _resolve_directory_entry(directory: Path, label: str) -> Path:
    from utils.transport import TransportError, resolve_entrypoint

    try:
        entry, _relative = resolve_entrypoint(directory)
    except TransportError as exc:
        raise InputError("%s无法定位入口：%s" % (label, exc)) from exc
    return entry


def resolve_input(task_root: Path, input_file: Optional[str],
                  allow_external: bool = False) -> Path:
    """定位输入入口：阶段内后续用 ``inputFile``，阶段首用上传 manifest。

    ``allow_external`` 仅供 CLI 使用：本地样例常位于任务目录之外，而 HTTP 层已用
    ``resolve_in_task`` 强制 ``inputFile`` 落在任务根内，因此默认（HTTP/run）保持严格。
    """
    if input_file:
        if not isinstance(input_file, str) or not input_file.strip():
            raise InputError("inputFile 不能为空")
        target: Optional[Path]
        try:
            # 只允许任务根内路径：跨 uniqueId / 任务根外一律拒绝
            target = io_utils.resolve_in_task(task_root, input_file)
        except ValueError as exc:
            if not allow_external:
                raise InputError("inputFile 非法：%s" % exc) from exc
            target = None
        if target is not None:
            if target.is_dir():
                return _resolve_directory_entry(target, "inputFile 目录")
            if target.is_file():
                return target
            if not allow_external:
                raise InputError("inputFile 不存在：%s" % Path(input_file).name)
        fallback = Path(input_file)
        if not fallback.is_absolute():
            fallback = BASE_DIR / fallback
        fallback = io_utils.resolved_absolute(fallback)
        if fallback.is_dir():
            return _resolve_directory_entry(fallback, "inputFile 目录")
        if not fallback.is_file():
            raise InputError("inputFile 不存在：%s" % Path(input_file).name)
        return fallback

    upload_dir = task_root / "input"
    if not upload_dir.is_dir():
        raise InputError("任务目录缺少 input/ 上传内容，且未提供 inputFile")
    return _resolve_directory_entry(upload_dir, "上传入口")


def load_stage_history(input_path: Path, task_root: Path) -> Tuple[List[dict], List[dict]]:
    """返回 ``(prior_lineage, stage_lineage)``。

    [v4.2] 血缘/阶段归档输出已停用：不再读取 stage_context.json / lineage_history.json，
    原实现（依赖 STAGE_CONTEXT_NAME / load_prior_lineage）注释存档。
    """
    # candidates = [input_path.parent, task_root / "input"]
    # for directory in candidates:
    #     context_path = directory / STAGE_CONTEXT_NAME
    #     if context_path.is_file():
    #         try:
    #             payload = json.loads(context_path.read_text(encoding="utf-8"))
    #         except (json.JSONDecodeError, OSError):
    #             continue
    #         if not isinstance(payload, dict):
    #             continue
    #         prior = [item for item in (payload.get("prior_lineage") or [])
    #                  if isinstance(item, dict)]
    #         stage = [item for item in (payload.get("stage_lineage") or [])
    #                  if isinstance(item, dict)]
    #         if stage or prior:
    #             return prior, stage
    # for directory in candidates:
    #     prior = load_prior_lineage(directory)
    #     if prior:
    #         return prior, []
    return [], []


# ---------------------------------------------------------------- 统计初值
def _initial_statistics(cfg: OperatorConfig) -> Dict[str, Any]:
    return {
        "input": 0, "input_lines": 0, "blank_lines": 0,
        "valid_records": 0, "bad_lines": 0,
        "admitted": 0, "returned": 0,
        "returned_description": 0, "returned_registration": 0,
        "origin_quarantined": 0, "origin_entries": 0, "sample_quarantined": 0,
        "locked": 0, "duplicates": 0,
        "description_missing": 0, "description_invalid": 0,
        "license_trainable": 0, "license_not_trainable": 0, "license_missing": 0,
        "files_checked": 0, "files_missing": 0, "files_broken": 0,
        "modality_counts": {}, "modality_pairs": {},
        "return_reasons": {}, "origin_reasons": {}, "sample_reasons": {},
        "media_copied": 0, "media_bytes": 0, "sidecar_files": 0, "artifact_files": 0,
        "file_check_mode": cfg.file_check_mode,
        "degraded_mode": False,
        "empty_input": False, "conservation_ok": True,
        "entrypoint": MAIN_NAME,
    }


def _bump(counter: Dict[str, Any], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def _output_root(output_root: Optional[str]) -> Path:
    """输出根：显式参数 > OUTPUT_DIR 环境变量 > 本目录 output。"""
    if output_root:
        return io_utils.resolve_root(output_root)
    env_root = os.environ.get("OUTPUT_DIR")
    if env_root:
        return io_utils.resolve_root(env_root)
    return io_utils.resolve_root(BASE_DIR / "output")


# ---------------------------------------------------------------- 媒体与旁路搬运
def _carry_media(records: List[dict], source_root: Path, run_dir: Path) -> List[str]:
    """把主清单仍引用的媒体按**原相对路径**复制到本次 run 目录。

    只有通过准入的记录才会被搬运：被隔离记录的媒体不复制，避免产生孤儿文件。
    """
    carried: List[str] = []
    for record in records:
        for name in ("image", "video", "audio"):
            reference = admission.media_reference(record, name)
            if not reference or reference in carried:
                continue
            try:
                source = io_utils.resolve_media(source_root, reference)
            except UnsupportedMediaPath:
                continue
            if not source.is_file():
                continue
            target = run_dir / reference
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve() != source.resolve():
                shutil.copyfile(source, target)
            carried.append(reference)
    return carried


def _carry_sidecars(source_root: Path, run_dir: Path) -> List[str]:
    """复制上游旁路清单（存在才复制），保留原文件名，避免重名覆盖。"""
    carried: List[str] = []
    if not source_root.is_dir():
        return carried
    reserved = set(OPERATOR_OUTPUT_NAMES)
    for path in sorted(source_root.iterdir()):
        if not path.is_file() or not SIDECAR_PATTERN.match(path.name):
            continue
        if path.name in reserved or path.name == "lineage_history.json":
            continue
        target = run_dir / path.name
        if target.resolve() == path.resolve() or target.exists():
            continue
        shutil.copyfile(path, target)
        carried.append(path.name)
    return carried


# ---------------------------------------------------------------- run
def run(input_file: Optional[str], unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """处理一次任务，返回内部 RunResult（dict）。

    ``status`` 取 success/partial/degraded/failed；``outputFile`` 为本地主清单路径
    （``10_admitted.jsonl``，合格待配对对象池），其余为三个旁路清单路径。
    [v4.2] 血缘输出已停用：不再返回 ``lineageFile``，也不写 ``lineage.json``/``stage_context.json``。
    HTTP 由 app.py 转成响应信封。
    """
    started = now_iso()
    cfg = load_config(config_path, overrides)
    root = _output_root(output_root)
    value = io_utils.validate_unique_id(unique_id)

    with io_utils.TaskLock(root, value):
        task = io_utils.task_dir(root, value)
        operator_dir = task / cfg.operator.name
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        logger = get_logger("%s.%s.%s" % (cfg.operator.name, value, run_id),
                            run_dir / "run.log")
        produced = False
        try:
            result = _execute(cfg, value, run_id, task, run_dir, input_file, started,
                              overrides, logger, allow_external_input)
            produced = True
            return result
        finally:
            # 先关日志再清理：Windows 上 run.log 句柄未释放会导致目录删不掉
            close_logger(logger)
            if not produced and not any((run_dir / name).exists()
                                       for name in OPERATOR_OUTPUT_NAMES):
                shutil.rmtree(run_dir, ignore_errors=True)


def _execute(cfg: OperatorConfig, unique_id: str, run_id: str, task: Path, run_dir: Path,
             input_file: Optional[str], started: str, overridden: Optional[dict], logger,
             allow_external_input: bool = False) -> Dict[str, Any]:
    operator = cfg.operator.name
    log_action(logger, "start", "operator=%s uniqueId=%s runId=%s stage=%s"
               % (operator, unique_id, run_id, cfg.stage_id))

    backends = admission.available_backends()
    log_action(logger, "backend", "image=%s video=%s audio=%s mode=%s missing=%s"
               % (backends.get("image_decoder"), backends.get("video_decoder"),
                  backends.get("audio_decoder"), cfg.file_check_mode,
                  ",".join(backends.get("missing") or []) or "none"))

    source = resolve_input(task, input_file, allow_external=allow_external_input)
    try:
        io_utils.assert_utf8(source)
    except UnicodeDecodeError as exc:
        raise InputError("输入不是严格 UTF-8：%s" % source.name) from exc
    log_action(logger, "input", "entry=%s bytes=%d" % (source.name, source.stat().st_size))

    # [v4.2] 血缘输出已停用：不再读取上游阶段血缘（代码保留注释存档）
    # prior_lineage, stage_records = load_stage_history(source, task)
    business = business_config(cfg)
    stats = _initial_statistics(cfg)
    stats["backends"] = backends

    main_path = run_dir / MAIN_NAME
    return_path = run_dir / RETURN_NAME
    origin_path = run_dir / ORIGIN_NAME
    sample_path = run_dir / SAMPLE_NAME
    main_tmp = run_dir / (MAIN_NAME + ".partial")
    return_tmp = run_dir / (RETURN_NAME + ".partial")
    origin_tmp = run_dir / (ORIGIN_NAME + ".partial")
    sample_tmp = run_dir / (SAMPLE_NAME + ".partial")

    seen_ids: set = set()
    admitted_records: List[dict] = []
    origin_entries = 0

    try:
        with io_utils.JsonlWriter(main_tmp) as main_writer, \
                io_utils.JsonlWriter(return_tmp) as return_writer, \
                io_utils.JsonlWriter(origin_tmp) as origin_writer, \
                io_utils.JsonlWriter(sample_tmp) as sample_writer:
            for lineno, line in enumerate(io_utils.iter_decoded_lines(source, "utf-8"), 1):
                stats["input_lines"] += 1
                if not line.strip():
                    stats["blank_lines"] += 1
                    continue
                stats["input"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    _bump(stats["origin_reasons"], "invalid_json")
                    origin_writer.write(admission.busy_line_entry(
                        operator=operator, stage_id=cfg.stage_id, line=lineno,
                        reason="invalid_json", message="JSON 解析失败，进入原始隔离区",
                        event_time=now_iso()))
                    continue
                if not isinstance(record, dict):
                    stats["bad_lines"] += 1
                    _bump(stats["origin_reasons"], "invalid_record")
                    origin_writer.write(admission.busy_line_entry(
                        operator=operator, stage_id=cfg.stage_id, line=lineno,
                        reason="invalid_record", message="顶层不是 JSON 对象，进入原始隔离区",
                        event_time=now_iso()))
                    continue

                stats["valid_records"] += 1
                decision = admission.evaluate_record(
                    record, lineno, source.parent, business, stats, seen_ids, now_iso(),
                    backends)
                if decision["action"] == admission.DECISION_ADMIT:
                    main_writer.write(decision["record"])
                    admitted_records.append(decision["record"])
                    stats["admitted"] += 1
                elif decision["action"] == admission.DECISION_RETURN:
                    return_writer.write(decision["entry"])
                    stats["returned"] += 1
                elif decision["action"] == admission.DECISION_ORIGIN:
                    origin_writer.write(decision["entry"])
                    stats["origin_quarantined"] += 1
                else:
                    sample_writer.write(decision["entry"])
                    stats["sample_quarantined"] += 1
            origin_entries = origin_writer.count
    except BaseException:
        io_utils.cleanup_files(main_tmp, return_tmp, origin_tmp, sample_tmp)
        raise

    os.replace(str(main_tmp), str(main_path))
    os.replace(str(return_tmp), str(return_path))
    os.replace(str(origin_tmp), str(origin_path))
    os.replace(str(sample_tmp), str(sample_path))

    stats["origin_entries"] = origin_entries
    media_files = _carry_media(admitted_records, source.parent, run_dir)
    sidecars = _carry_sidecars(source.parent, run_dir)
    stats["media_copied"] = len(media_files)
    stats["media_bytes"] = sum((run_dir / name).stat().st_size for name in media_files
                               if (run_dir / name).is_file())
    stats["sidecar_files"] = len(sidecars)

    expected_origin = stats["bad_lines"] + stats["origin_quarantined"]
    stats["conservation_ok"] = (
        stats["input"] == stats["valid_records"] + stats["bad_lines"]
        and stats["valid_records"] == (stats["admitted"] + stats["returned"]
                                       + stats["origin_quarantined"]
                                       + stats["sample_quarantined"])
        and origin_entries == expected_origin
    )
    if not stats["conservation_ok"]:
        io_utils.cleanup_files(main_path, return_path, origin_path, sample_path)
        raise InputError(
            "统计不守恒：input=%d valid=%d bad=%d admitted=%d returned=%d "
            "origin=%d sample=%d origin_entries=%d"
            % (stats["input"], stats["valid_records"], stats["bad_lines"],
               stats["admitted"], stats["returned"], stats["origin_quarantined"],
               stats["sample_quarantined"], origin_entries))

    if stats["input"] == 0:
        stats["empty_input"] = True
        status, message = "success", "empty_input：输入没有任何数据行"
    elif stats["degraded_mode"]:
        status, message = "degraded", "degraded：解码器缺失，相关文件只做了头签名校验"
    elif stats["origin_quarantined"] or stats["sample_quarantined"] or stats["bad_lines"]:
        status, message = "partial", ("partial：原始隔离 %d 条、样本隔离 %d 条、坏行 %d 条"
                                      % (stats["origin_quarantined"],
                                         stats["sample_quarantined"], stats["bad_lines"]))
    else:
        status, message = "success", "success"

    artifacts: List[Dict[str, str]] = [
        {"path": MAIN_NAME, "role": "main"},
        {"path": RETURN_NAME, "role": "review"},
        {"path": ORIGIN_NAME, "role": "review"},
        {"path": SAMPLE_NAME, "role": "review"},
    ]
    reserved = {item["path"] for item in artifacts}
    artifacts.extend({"path": relative, "role": "media"}
                     for relative in media_files if relative not in reserved)
    artifacts.extend({"path": name, "role": "sidecar"} for name in sidecars
                     if name not in reserved)
    missing = [item["path"] for item in artifacts if not (run_dir / item["path"]).is_file()]
    if missing:
        raise InputError("输出产物缺失：%s" % ",".join(sorted(missing)))
    stats["artifact_files"] = len(artifacts)

    manifest = build_manifest(MAIN_NAME, artifacts)
    manifest.update({
        "operator": operator,
        "formal_operator_numbers": [FORMAL_OPERATOR_NUMBER],
        "run_id": run_id,
        "unique_id": unique_id,
        "created_at": now_iso(),
        "stage_id": cfg.stage_id,
        "notes": ("表1 #10 准入核对：合格待配对对象交 #11；退回清单交 #9/批次登记；"
                  "原始隔离台账与样本隔离台账交 #21；纯规则，无模型分数"),
    })
    write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    finished = now_iso()
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录，不再写 lineage.json /
    # stage_context.json（原 make_lineage/build_stage_context 调用块注释存档）
    # stats_for_lineage = dict(stats)
    # stats_for_lineage.pop("backends", None)
    # record = make_lineage(
    #     operator=operator, version=cfg.operator.version, unique_id=unique_id,
    #     stage_id=cfg.stage_id, run_id=run_id, input_file=str(source),
    #     output_file=str(main_path), status=status, started_at=started,
    #     finished_at=finished, config=config_snapshot(cfg, overridden),
    #     statistics=stats_for_lineage, artifacts=artifacts,
    #     formal_operator_number=FORMAL_OPERATOR_NUMBER)
    # stage_lineage = list(stage_records) + [record]
    # io_utils.atomic_write_json(run_dir / "lineage.json", list(prior_lineage) + stage_lineage)
    # io_utils.atomic_write_json(run_dir / STAGE_CONTEXT_NAME, build_stage_context(
    #     stage_id=cfg.stage_id, unique_id=unique_id, run_id=run_id,
    #     entrypoint=MAIN_NAME, stage_lineage=stage_lineage, prior_lineage=prior_lineage))

    log_action(logger, "statistics",
               "input=%d valid=%d bad=%d admitted=%d returned=%d origin=%d sample=%d "
               "locked=%d files=%d media=%d"
               % (stats["input"], stats["valid_records"], stats["bad_lines"],
                  stats["admitted"], stats["returned"], stats["origin_quarantined"],
                  stats["sample_quarantined"], stats["locked"], stats["files_checked"],
                  stats["media_copied"]))
    log_action(logger, "finish", "status=%s runId=%s" % (status, run_id))

    return {
        "status": status,
        "outputFile": str(main_path),
        "returnFile": str(return_path),
        "originQuarantineFile": str(origin_path),
        "sampleQuarantineFile": str(sample_path),
        # [v4.2] 血缘输出已停用：lineageFile/stageLineage/priorLineage 键不再回传
        # "lineageFile": str(run_dir / "lineage.json"),
        "manifestPath": str(run_dir / "manifest.json"),
        "statistics": stats,
        "message": message,
        "runId": run_id,
        "runDir": str(run_dir),
        "entrypoint": MAIN_NAME,
        "artifacts": artifacts,
        # "stageLineage": stage_lineage,
        # "priorLineage": prior_lineage,
        "uniqueId": unique_id,
        "operator": operator,
        "backends": backends,
    }


# ---------------------------------------------------------------- 响应形态
# [v4.2] 血缘输出已停用：build_intermediate_lineage/build_stage_payload 不再被
# app.py / CLI 调用（原实现依赖 result["stageLineage"]，注释存档）
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """中间态响应用的“当前节点”血缘（含临时 ``outputFile`` 本地快照）。"""
#     return copy.deepcopy(result["stageLineage"][-1])
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末对外血缘：当前阶段完整数组，剔除临时绝对路径。"""
#     return to_stage_lineage(result["stageLineage"])


def stage_file_object(result: Dict[str, Any],
                      max_response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """阶段末文件内容对象：四类清单 + 媒体 + 上游旁路清单 + 运行清单。"""
    limit = MAX_RESPONSE_BYTES if max_response_bytes is None else int(max_response_bytes)
    paths = [item["path"] for item in result["artifacts"]]
    if (Path(result["runDir"]) / MANIFEST_NAME).is_file():
        paths.append(MANIFEST_NAME)
    return atomic_pack_file_object(result["runDir"], result["entrypoint"], paths,
                                   max_response_bytes=limit)


# ---------------------------------------------------------------- CLI
def _emit(payload: Dict[str, Any]) -> None:
    """CLI 结构化输出：优先 UTF-8，失败退回 ASCII 转义，保证始终可解析。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    sys.stdout.write(text + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="算子 10：核对输入对象准入")
    parser.add_argument("--input", required=True, help="输入 JSONL 完整记录清单（#9 输出）")
    parser.add_argument("--unique-id", required=True, dest="unique_id", help="任务id_文件id")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根目录（默认 OUTPUT_DIR 或本目录 output）")
    parser.add_argument("--config", default=None, help="YAML 配置路径")
    parser.add_argument("--override", default=None,
                        help="业务参数覆盖，JSON 字符串，例如 {\"min_modalities\":3}")
    parser.add_argument("--is-last", dest="is_last", action="store_true",
                        help="按阶段末算子预览 data 形状（outputFile=file-object）")
    parser.add_argument("--quiet", action="store_true", help="只输出核心摘要")
    return parser


def _parse_overrides(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("--override 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError("--override 必须是 JSON 对象")
    return payload


def cli_data_shape(status: str, is_last: bool) -> Dict[str, str]:
    """CLI 摘要里对 ``/predict`` 响应 ``data`` 形状的如实描述。

    [v4.2] 形态二统一响应：始终返回 ``crossmodal-files-v1`` 文件内容对象 ``outputFile``，
    不再返回 ``lineageFile``；``is_last`` 仅保留参数兼容。
    """
    if status == "failed":
        return {}
    return {"outputFile": "file-object"}


def _summary(result: Dict[str, Any], is_last: bool, quiet: bool) -> Dict[str, Any]:
    stats = result["statistics"]
    summary: Dict[str, Any] = {
        "status": result["status"],
        "message": result["message"],
        "uniqueId": result.get("uniqueId"),
        "runId": result["runId"],
        "input": stats["input"],
        "inputLines": stats["input_lines"],
        "validRecords": stats["valid_records"],
        "badLines": stats["bad_lines"],
        "admitted": stats["admitted"],
        "returned": stats["returned"],
        "originQuarantined": stats["origin_quarantined"],
        "sampleQuarantined": stats["sample_quarantined"],
        "locked": stats["locked"],
        "emptyInput": stats["empty_input"],
        "conservationOk": stats["conservation_ok"],
        "entrypoint": result["entrypoint"],
        "dataShape": cli_data_shape(result["status"], is_last),
    }
    if not quiet:
        summary.update({
            "outputFile": result["outputFile"],
            "returnFile": result["returnFile"],
            "originQuarantineFile": result["originQuarantineFile"],
            "sampleQuarantineFile": result["sampleQuarantineFile"],
            "runDir": result["runDir"],
        })
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or str(default_config_path())
    try:
        overrides = _parse_overrides(args.override)
    except ConfigError as exc:
        _emit({"code": 1, "msg": str(exc), "data": {}})
        return 1

    try:
        result = run(args.input, args.unique_id, config_path=config_path,
                     output_root=args.output_root, overrides=overrides,
                     allow_external_input=True)
    except (ConfigError, InputError, DependencyError, io_utils.TaskBusyError,
            OSError, ValueError) as exc:
        _emit({"code": 1, "msg": "%s: %s" % (type(exc).__name__, exc), "data": {}})
        return 1

    code = 0 if result["status"] in ("success", "partial", "degraded") else 1
    _emit({"code": code, "msg": result["message"],
           "data": _summary(result, args.is_last, args.quiet)})
    return code


if __name__ == "__main__":
    sys.exit(main())
