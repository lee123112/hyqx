"""算子 21（处置错配样本）编排入口：run() + CLI。

职责：
1. 加载并快照部署配置（``configs/config.yaml``），处理允许的业务参数覆盖；
2. 严格校验 ``uniqueId``、入口清单、UTF-8 编码、必需字段与模态引用；
3. 流式读取上游台账条目/完整记录，逐条调用 ``utils.ledger`` 做处置判定；
4. 输出可查询隔离台账、原始/样本隔离清单、回流指令、人工队列清单与拒绝清单；
5. 生成运行清单、血缘与阶段上下文，并做守恒校验；
6. 需要时把放行样本的媒体按原相对路径原子搬运到运行目录。

表1 冻结口径由 ``utils.ledger`` 实现；本文件只做编排与校验，不重复业务规则。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
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

from utils import io as io_utils
# [v4.2] 血缘输出已停用：不再导入 lineage 模块（utils/lineage.py 保留存档）
from utils import ledger, logger as log_utils, transport
from utils.schemas import (
    DEPLOYMENT_KEYS,
    READONLY_KEYS,
    BusinessOverrides,
    ConfigError,
    OperatorConfig,
)

_TZ = timezone(timedelta(hours=8))
OPERATOR_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = OPERATOR_DIR / "configs" / "config.yaml"

#: 主产物（放行给 #22）与配套产物名
MAIN_NAME = "21_passed.jsonl"
LEDGER_NAME = "21_ledger.jsonl"
ORIGIN_NAME = "21_origin_quarantine.jsonl"
SAMPLE_NAME = "21_sample_quarantine.jsonl"
RETURN_NAME = "21_return.jsonl"
PENDING_NAME = "21_pending_manual.jsonl"
REJECT_NAME = "21_rejects.jsonl"

OUTPUT_NAMES = (MAIN_NAME, LEDGER_NAME, ORIGIN_NAME, SAMPLE_NAME, RETURN_NAME,
                PENDING_NAME, REJECT_NAME)

#: 业务产物 → manifest 角色
ARTIFACT_ROLES = {
    MAIN_NAME: "main",
    LEDGER_NAME: "review",
    ORIGIN_NAME: "review",
    SAMPLE_NAME: "review",
    RETURN_NAME: "review",
    PENDING_NAME: "review",
    REJECT_NAME: "review",
}

#: 媒体引用字段（只搬运放行样本的媒体；隔离样本媒体一律不搬运）
MEDIA_FIELDS = ("image", "audio", "video")

#: 处理中的临时后缀（失败或守恒校验不通过时清理，避免半截文件被下游读到）
PARTIAL_SUFFIX = ".partial"


class InputError(ValueError):
    """入参、输入内容或结果守恒不满足契约。"""


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 配置
def load_config(config_path: Optional[str] = None) -> OperatorConfig:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("配置文件 YAML 解析失败：%s" % type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是映射")
    try:
        return OperatorConfig(**raw)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError 及同族异常
        raise ConfigError("配置校验失败：%s" % exc) from exc


def apply_overrides(config: OperatorConfig, overrides: Optional[Dict[str, Any]],
                    config_path: Optional[str]) -> tuple:
    """合并请求覆盖，返回 ``(新配置, 实际生效的覆盖键)``。"""
    if not overrides:
        return config, []
    for key in overrides:
        if key in DEPLOYMENT_KEYS:
            raise ConfigError("请求不能改写部署身份字段：config.%s" % key)
        if key in READONLY_KEYS:
            raise ConfigError("请求不能覆盖表1 只读配置：config.%s" % key)
    try:
        parsed = BusinessOverrides(**overrides)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError("请求 config 校验失败：%s" % exc) from exc
    provided = parsed.provided()
    if not provided:
        return config, []
    base = load_config(config_path).model_dump()
    base.update(provided)
    try:
        updated = OperatorConfig(**base)
    except Exception as exc:
        raise ConfigError("覆盖后的配置不合法：%s" % exc) from exc
    return updated, sorted(provided)


def config_snapshot(config: OperatorConfig, config_path: Optional[str],
                    overridden_keys: List[str]) -> Dict[str, Any]:
    return {
        "configPath": str(Path(config_path).expanduser() if config_path
                          else DEFAULT_CONFIG_PATH),
        "deployment": config.deployment_dict(),
        "business": config.business_dict(),
        "overridden_keys": list(overridden_keys),
    }


def business_config(config: OperatorConfig) -> Dict[str, Any]:
    """交给 ``utils.ledger`` 的业务参数字典（含算子身份与阶段）。"""
    payload = config.business_dict()
    payload.update({
        "operator": config.operator.name,
        "operator_version": config.operator.version,
        "stage_id": config.stage_id,
    })
    return payload


# ------------------------------------------------------------------ 输入定位
def resolve_input(task_root: Path, input_file, allow_external: bool) -> Path:
    """把 ``inputFile`` 定位到 realpath；跨任务/越界一律拒绝（符号链接按 realpath）。"""
    if not input_file:
        raise InputError("缺少 inputFile：阶段内后续算子必须传上一中间响应中的输入路径")
    root = io_utils.resolved_absolute(task_root)
    if allow_external:
        target = io_utils.resolved_absolute(input_file)
        if "\x00" in str(input_file):
            raise InputError("inputFile 含非法字符")
        if not target.exists():
            raise InputError("inputFile 不存在：%s" % target)
        return target
    target = io_utils.resolve_in_task(root, str(input_file))
    if not target.exists():
        raise InputError("inputFile 不存在：%s" % target)
    return target


# [v4.2] 血缘输出已停用：历史血缘读取不再参与运行（utils/lineage.py 保留存档）。
# def load_stage_history(input_path: Path):
#     """返回 ``(已归档的历史阶段血缘, 本阶段已累积血缘)``，两者都只读不改。
#
#     - 历史阶段血缘来自随包回传的 ``lineage_history.json``（工程侧归档，本算子不改写）；
#     - 本阶段已累积血缘由阶段内链式调用产生：上一中间响应的 ``inputFile`` 所在运行目录
#       内留有该次 ``stage_context.json``，据此把同一 uniqueId 的阶段血缘连续累积。
#     """
#     candidates = [input_path] if input_path.is_dir() else [input_path.parent]
#     if not input_path.is_dir():
#         candidates.append(input_path.parent.parent)
#     history = lineage.load_prior_lineage(candidates)
#     prior_stage = lineage.load_prior_stage_lineage(candidates)
#     return history, prior_stage


# ------------------------------------------------------------------ 媒体搬运
def carry_media(records: List[Dict[str, Any]], source_root: Path, run_dir: Path,
                stats: Dict[str, Any]) -> List[Dict[str, str]]:
    """只搬运**放行样本**的媒体；隔离样本的媒体绝不复制（禁止负样本工厂）。"""
    artifacts: List[Dict[str, str]] = []
    seen = set()
    for record in records:
        for name in MEDIA_FIELDS:
            reference = ledger.media_reference(record, name)
            if not reference:
                continue
            try:
                source = io_utils.resolve_media(source_root, reference)
            except io_utils.UnsupportedMediaPath:
                ledger.bump(stats, "media_missing")
                continue
            if not source.is_file():
                ledger.bump(stats, "media_missing")
                continue
            if reference in seen:
                continue
            seen.add(reference)
            target = io_utils.resolve_media(run_dir, reference)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            ledger.bump(stats, "media_carried")
            ledger.bump(stats, "media_bytes", target.stat().st_size)
            artifacts.append({"path": reference, "role": name})
    return artifacts


# ------------------------------------------------------------------ 主流程
def run(input_file: Optional[str], unique_id: str, *, config_path: Optional[str] = None,
        output_root=None, overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次算子 21 处置，返回结构化结果（含主产物真实路径与统计）。"""
    if output_root is None:
        output_root = os.environ.get("OUTPUT_DIR") or (OPERATOR_DIR / "output")
    root = io_utils.resolve_root(output_root)
    io_utils.validate_unique_id(unique_id)

    config = load_config(config_path)
    config, overridden_keys = apply_overrides(config, overrides, config_path)
    business = business_config(config)
    stats = ledger.initial_statistics()
    stats["manual_platform_available"] = config.manual_platform_available
    stats["entrypoint"] = MAIN_NAME

    task = io_utils.task_dir(root, unique_id)
    started_at = now_iso()

    with io_utils.TaskLock(root, unique_id):
        operator_dir = task / config.operator.name
        operator_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        run_log = log_utils.get_logger("operator21", run_dir / "run.log")
        try:
            log_utils.log_action(run_log, "start",
                                 "operator=%s version=%s uniqueId=%s run=%s"
                                 % (config.operator.name, config.operator.version,
                                    unique_id, run_id))
            result = _execute(run_dir, task, input_file, config, business, stats,
                              log_utils, run_log, allow_external_input)

            stats["conservation_ok"] = _check_conservation(stats)
            if not stats["conservation_ok"]:
                raise InputError("结果守恒校验失败：%s" % json.dumps(
                    _conservation_view(stats), ensure_ascii=False))
            status = _resolve_status(stats)
            stats["status"] = status

            log_utils.log_action(run_log, "counts",
                                 "input_lines=%d blank_lines=%d input_records=%d "
                                 "valid_records=%d bad_lines=%d released=%d"
                                 % (stats["input_lines"], stats["blank_lines"],
                                    stats["input_records"], stats["valid_records"],
                                    stats["bad_lines"], stats["released"]))
            log_utils.log_action(run_log, "dispose",
                                 "origin=%d sample=%d locked=%d returned=%d "
                                 "waiting_manual=%d pending_process=%d"
                                 % (stats["origin_isolated"], stats["sample_isolated"],
                                    stats["locked"], stats["returned"],
                                    stats["waiting_manual"], stats["pending_process"]))
            log_utils.log_action(run_log, "finish",
                                 "status=%s ledger_entries=%d main=%d pending=%d"
                                 % (status, stats["ledger_entries"], stats["main_entries"],
                                    stats["pending_entries"]))

            # 原子提交：先写临时名，全部成功后改名，避免半截文件被下游读到
            final_paths: List[Path] = []
            for name in OUTPUT_NAMES:
                partial = run_dir / (name + PARTIAL_SUFFIX)
                target = run_dir / name
                if partial.exists():
                    os.replace(str(partial), str(target))
                else:
                    target.write_text("", encoding="utf-8")
                final_paths.append(target)
            log_utils.close_logger(run_log)

            finished_at = now_iso()
            result["outputFile"] = str(run_dir / MAIN_NAME)
            result["statistics"] = stats
            result["status"] = status
            result["startedAt"] = started_at
            result["finishedAt"] = finished_at
            result["config"] = config_snapshot(config, config_path, overridden_keys)

            artifacts = result.pop("artifacts")
            # [v4.2] 血缘输出已停用：本地血缘节点、阶段血缘累积与阶段上下文写入全部停用
            # （utils/lineage.py 保留存档，不再调用）。
            # node = lineage.make_lineage(
            #     operator=config.operator.name, version=config.operator.version,
            #     unique_id=unique_id, stage_id=config.stage_id, run_id=run_id,
            #     input_file=result["inputFile"], output_file=str(run_dir / MAIN_NAME),
            #     status=status, started_at=started_at, finished_at=finished_at,
            #     config=result["config"], statistics=stats, artifacts=artifacts,
            #     formal_operator_number=ledger.FORMAL_OPERATOR_NUMBER)
            # stage_lineage = lineage.append_lineage(run_dir / lineage.LINEAGE_NAME, node,
            #                                        previous=result.pop("priorStageLineage"))
            # prior = result.pop("priorLineage")
            # context = lineage.build_stage_context(
            #     config.stage_id, unique_id, run_id, MAIN_NAME,
            #     lineage.to_stage_lineage(stage_lineage), prior)
            # io_utils.atomic_write_json(run_dir / lineage.STAGE_CONTEXT_NAME, context)
            # [v4.2] 血缘输出已停用：_process 不再返回 priorLineage/priorStageLineage，
            # 因此无需再从 result 中剔除这两个键。

            manifest = transport.build_manifest(MAIN_NAME, artifacts)
            manifest.update({
                "operator": config.operator.name,
                "operator_version": config.operator.version,
                "formal_operator_numbers": [ledger.FORMAL_OPERATOR_NUMBER],
                "stage_id": config.stage_id,
                "unique_id": unique_id,
                "run_id": run_id,
                "created_at": finished_at,
                "notes": ("隔离样本媒体一律不复制（禁止负样本工厂）；"
                          "评分分层/流程状态不进隔离区；"
                          "人工平台未接入时需人工确认的记录按 waiting_manual 留产线"),
            })
            transport.write_manifest(run_dir, manifest)
            publish_stage_output(run_dir)

            result.update({
                "runDir": str(run_dir),
                "runId": run_id,
                "entrypoint": MAIN_NAME,
                "artifacts": artifacts,
                # [v4.2] 血缘输出已停用：run() 返回不再包含 stageLineage/localNode/
                # priorLineage 等血缘键。
            })
            return result
        except BaseException:
            log_utils.close_logger(run_log)
            for name in OUTPUT_NAMES:
                io_utils.cleanup_files(run_dir / name, run_dir / (name + PARTIAL_SUFFIX))
            # 未产出任何正式产物时清理空 run 目录，不留垃圾
            if run_dir.exists():
                leftovers = [child for child in run_dir.iterdir()
                             if child.name not in ("manifest.json", "run.log")]
                if not leftovers:
                    io_utils.cleanup_files(run_dir / "manifest.json", run_dir / "run.log")
                    try:
                        run_dir.rmdir()
                    except OSError:
                        pass
            raise


def _execute(run_dir: Path, task: Path, input_file: Optional[str],
             config: OperatorConfig, business: Dict[str, Any], stats: Dict[str, Any],
             log_module, run_log, allow_external_input: bool) -> Dict[str, Any]:
    source = resolve_input(task, input_file, allow_external_input)
    if source.is_dir():
        entrypoint, _relative = transport.resolve_entrypoint(source)
    else:
        entrypoint = source
    log_module.log_action(run_log, "input", "source=%s" % entrypoint.name)

    # 严格 UTF-8 预检：编码错误按契约直接失败，不猜测编码
    try:
        io_utils.assert_utf8(entrypoint)
    except UnicodeDecodeError as exc:
        raise InputError("输入不是严格 UTF-8（偏移 %d）" % exc.start) from exc

    prior, prior_stage = None, None  # [v4.2] 血缘输出已停用：不再读取历史血缘
    # [v4.2] 原调用：prior, prior_stage = load_stage_history(source)

    main_path = run_dir / (MAIN_NAME + PARTIAL_SUFFIX)
    ledger_path = run_dir / (LEDGER_NAME + PARTIAL_SUFFIX)
    origin_path = run_dir / (ORIGIN_NAME + PARTIAL_SUFFIX)
    sample_path = run_dir / (SAMPLE_NAME + PARTIAL_SUFFIX)
    return_path = run_dir / (RETURN_NAME + PARTIAL_SUFFIX)
    pending_path = run_dir / (PENDING_NAME + PARTIAL_SUFFIX)
    reject_path = run_dir / (REJECT_NAME + PARTIAL_SUFFIX)

    seen_ids: set = set()
    main_records: List[Dict[str, Any]] = []

    with io_utils.JsonlWriter(main_path) as main_writer, \
            io_utils.JsonlWriter(ledger_path) as ledger_writer, \
            io_utils.JsonlWriter(origin_path) as origin_writer, \
            io_utils.JsonlWriter(sample_path) as sample_writer, \
            io_utils.JsonlWriter(return_path) as return_writer, \
            io_utils.JsonlWriter(pending_path) as pending_writer, \
            io_utils.JsonlWriter(reject_path) as reject_writer:

        for lineno, raw_line in enumerate(
                io_utils.iter_decoded_lines(entrypoint, "utf-8"), start=1):
            ledger.bump(stats, "input_lines")
            if not raw_line.strip():
                ledger.bump(stats, "blank_lines")
                continue
            ledger.bump(stats, "input_records")
            event_time = now_iso()

            record: Any = None
            reject_reason = None
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                reject_reason = ("invalid_json", "JSON 解析失败")
            if reject_reason is None and not isinstance(record, dict):
                reject_reason = ("invalid_record", "记录必须是 JSON 对象")
            if reject_reason is None:
                sample_id = record.get("sample_id")
                if not isinstance(sample_id, str) or not sample_id.strip():
                    reject_reason = ("missing_sample_id", "缺少 sample_id")

            if reject_reason is not None:
                reason, message = reject_reason
                ledger.bump(stats, "bad_lines")
                ledger.bump(stats["reason_counts"], reason)
                ledger.bump(stats["label_counts"], ledger.LABEL_UNPARSEABLE)
                ledger.bump(stats["category_counts"], "reject")
                entry = ledger.busy_line_entry(
                    operator=config.operator.name, stage_id=config.stage_id,
                    line=lineno, reason=reason, message=message, event_time=event_time)
                reject_writer.write(entry)
                ledger_writer.write(entry)
                continue

            ledger.bump(stats, "valid_records")
            outcome = ledger.evaluate_record(record, lineno, business, stats, seen_ids,
                                             event_time)
            ledger_writer.write(outcome["entry"])
            route = outcome["route"]
            if route == ledger.ROUTE_RELEASED:
                main_writer.write(outcome["record"])
                main_records.append(outcome["record"])
            elif route == ledger.ROUTE_ORIGIN:
                origin_writer.write(outcome["entry"])
            elif route == ledger.ROUTE_SAMPLE:
                sample_writer.write(outcome["entry"])
            elif route == ledger.ROUTE_RETURN:
                return_writer.write(outcome["entry"])
            elif route == ledger.ROUTE_PENDING:
                pending_writer.write(outcome["entry"])
                main_writer.write(outcome["record"])
                main_records.append(outcome["record"])
            else:  # pragma: no cover - 路由枚举由 ledger 保证
                raise InputError("未知处置路由：%s" % route)

    stats["main_entries"] = main_writer.count
    stats["ledger_entries"] = ledger_writer.count
    stats["origin_entries"] = origin_writer.count
    stats["sample_entries"] = sample_writer.count
    stats["return_entries"] = return_writer.count
    stats["pending_entries"] = pending_writer.count
    stats["reject_entries"] = reject_writer.count
    stats["empty_input"] = stats["input_records"] == 0

    media_artifacts = carry_media(main_records, entrypoint.parent, run_dir, stats)
    artifacts = [{"path": name, "role": ARTIFACT_ROLES[name]} for name in OUTPUT_NAMES]
    artifacts.extend(media_artifacts)

    # [v4.2] 血缘输出已停用：不再回传 priorLineage/priorStageLineage
    return {"inputFile": str(entrypoint), "artifacts": artifacts}


def _conservation_view(stats: Dict[str, Any]) -> Dict[str, int]:
    keys = ("input_records", "blank_lines", "valid_records", "bad_lines", "released",
            "passed", "process_kept", "not_applicable", "waiting_manual", "pending_process",
            "origin_isolated", "sample_isolated", "returned", "locked", "main_entries",
            "ledger_entries", "origin_entries", "sample_entries", "return_entries",
            "pending_entries", "reject_entries", "isolated_media_carried")
    return {key: int(stats.get(key, 0)) for key in keys}


def _check_conservation(stats: Dict[str, Any]) -> bool:
    """守恒校验：每条记录只能有一个去处，台账与各清单必须逐项对齐。"""
    checks = [
        stats["input_records"] == stats["valid_records"] + stats["bad_lines"],
        stats["valid_records"] == (stats["released"] + stats["origin_isolated"]
                                   + stats["sample_isolated"] + stats["returned"]),
        stats["released"] == (stats["passed"] + stats["process_kept"]
                              + stats["not_applicable"] + stats["waiting_manual"]),
        stats["sample_isolated"] >= stats["locked"],
        stats["process_kept"] >= stats["pending_process"],
        stats["main_entries"] == stats["released"],
        stats["ledger_entries"] == stats["valid_records"] + stats["bad_lines"],
        stats["origin_entries"] == stats["origin_isolated"],
        stats["sample_entries"] == stats["sample_isolated"],
        stats["return_entries"] == stats["returned"],
        stats["pending_entries"] == stats["waiting_manual"] + stats["pending_process"],
        stats["reject_entries"] == stats["bad_lines"],
        stats["isolated_media_carried"] == 0,
    ]
    return all(checks)


def _resolve_status(stats: Dict[str, Any]) -> str:
    """运行状态：degraded 优先于 partial，partial 优先于 success。"""
    if stats["input_records"] == 0:
        return "success"
    if stats.get("degraded_mode"):
        return "degraded"
    if (stats["origin_isolated"] or stats["sample_isolated"] or stats["returned"]
            or stats["bad_lines"]):
        return "partial"
    return "success"


# ------------------------------------------------------------------ 阶段返回形态
# [v4.2] 血缘输出已停用：lineageFile 返回形态不再使用（响应统一见 app.py §3 改造）。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """阶段内中间算子返回形态：仅当前 ``lineageFile`` 对象，含临时 outputFile。"""
#     return result["localNode"]
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末返回形态：完整阶段血缘数组（已移除临时 outputFile）。"""
#     return lineage.to_stage_lineage(result["stageLineage"])


def stage_file_object(result: Dict[str, Any],
                      max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """阶段末最终产物文件内容对象（主产物 + 台账 + 清单 + 放行样本媒体）。"""
    limit = max_bytes
    if limit is None:
        limit = int(result.get("config", {}).get("business", {})
                    .get("transport", {}).get("max_response_bytes", 67108864))
    paths = [MAIN_NAME]
    for artifact in result["artifacts"]:
        if artifact["path"] not in paths:
            paths.append(artifact["path"])
    if (Path(result["runDir"]) / transport.MANIFEST_NAME).is_file():
        paths.append(transport.MANIFEST_NAME)
    return transport.pack_file_object(result["runDir"], MAIN_NAME, paths,
                                      max_response_bytes=limit)


# ------------------------------------------------------------------ CLI
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 21 处置错配样本：统一台账 + 原始/样本隔离 + 回流/锁定")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径或素材目录")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识")
    parser.add_argument("--config", default=None, help="部署配置 YAML 路径")
    parser.add_argument("--output-root", default=None, help="输出根目录（默认 $OUTPUT_DIR）")
    parser.add_argument("--override", default=None,
                        help='JSON 字符串，覆盖允许的业务参数（如 {"return_limit": 2}）')
    parser.add_argument("--strict-input", action="store_true",
                        help="把 --input 限制在 OUTPUT_DIR/<uniqueId>/ 内；"
                             "默认关闭（CLI 是本地工具，可直接读样例路径，"
                             "服务化调用请走 HTTP 层，那里始终强制任务内寻址）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    overrides = None
    if args.override:
        try:
            overrides = json.loads(args.override)
        except json.JSONDecodeError as exc:
            print(json.dumps({"code": 1, "msg": "override 不是合法 JSON：%s"
                              % type(exc).__name__, "data": {}}, ensure_ascii=False))
            return 2
        if not isinstance(overrides, dict):
            print(json.dumps({"code": 1, "msg": "override 必须是 JSON 对象", "data": {}},
                             ensure_ascii=False))
            return 2
    try:
        result = run(input_file=args.input, unique_id=args.unique_id,
                     config_path=args.config, output_root=args.output_root,
                     overrides=overrides,
                     allow_external_input=not args.strict_input)
    except (ConfigError, InputError, ledger.DependencyError,
            io_utils.TaskBusyError, transport.TransportError) as exc:
        print(json.dumps({"code": 1, "msg": "%s: %s" % (type(exc).__name__, exc),
                          "data": {}}, ensure_ascii=False))
        return 1
    except ValueError as exc:
        print(json.dumps({"code": 1, "msg": "ValueError: %s" % exc, "data": {}},
                         ensure_ascii=False))
        return 1

    summary = {
        "status": result["status"],
        "operator": result["config"]["deployment"]["operator"]["name"],
        "uniqueId": args.unique_id,
        "runId": result["runId"],
        "inputFile": result["inputFile"],
        "outputFile": result["outputFile"],
        "statistics": result["statistics"],
        # [v4.2] 血缘输出已停用：CLI 摘要不再回传 lineageFile
        "artifacts": result["artifacts"],
    }
    print(json.dumps({"code": 0, "msg": result["status"], "data": summary},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
