"""算子 #02（核验场景编号）编排入口：``run()`` + CLI。

职责：
1. 加载并快照部署配置（``configs/config.yaml``），处理允许的业务参数覆盖；
2. 严格校验 ``uniqueId``、入口清单、UTF-8 编码与 ``sample_id``；
3. 流式读取上游记录，逐条校验并解析场景编号绑定（``utils.scene``）；
4. 按场景编号聚合、核验同编号模态数，产出融合集场景组与改走配对集清单；
5. 生成运行清单、血缘、阶段上下文与运行级报告，并做守恒校验；
6. 需要时把主产物引用到的媒体按原相对路径原子搬运到运行目录。

表1 #02 冻结口径由 ``utils.scene`` 实现；本文件只做编排与校验，不重复业务规则。
本算子**无模型、无权重、不联网**，不存在伪造的分数或降级结论。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from utils import io as io_utils
# [v4.2] 血缘/阶段归档输出已停用：不再导入 lineage（仅保留 io/logger/scene/transport）
# from utils import lineage, logger as log_utils, scene, transport
from utils import logger as log_utils, scene, transport
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

#: 产物名（来自业务模块，单一事实来源）
MAIN_NAME = scene.MAIN_NAME
GROUPS_NAME = scene.GROUPS_NAME
PAIRING_NAME = scene.PAIRING_NAME
ORIGIN_NAME = scene.ORIGIN_NAME
REJECT_NAME = scene.REJECT_NAME
REPORT_NAME = scene.REPORT_NAME
OUTPUT_NAMES = scene.OUTPUT_NAMES

#: 处理中的临时后缀（失败或守恒校验不通过时清理，避免半截文件被下游读到）
PARTIAL_SUFFIX = ".partial"

#: 媒体引用字段（三种媒体按原相对路径搬运；缺路径或越界不伪造文件）
MEDIA_FIELDS = ("image", "video", "audio")


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
        if key in DEPLOYMENT_KEYS or key in READONLY_KEYS:
            raise ConfigError("请求不能改写部署身份或表1 只读项：config.%s" % key)
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
    """交给 ``utils.scene`` 的业务参数字典（含算子身份与阶段）。"""
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
    if allow_external:
        if "\x00" in str(input_file):
            raise InputError("inputFile 含非法字符")
        target = io_utils.resolved_absolute(input_file)
        if not target.exists():
            raise InputError("inputFile 不存在：%s" % target)
        return target
    root = io_utils.resolved_absolute(task_root)
    target = io_utils.resolve_in_task(root, str(input_file))
    if not target.exists():
        raise InputError("inputFile 不存在：%s" % target)
    return target


def load_stage_history(input_path: Path):
    """返回 ``(已归档的历史阶段血缘, 本阶段已累积血缘)``，两者都只读不改。"""
    # [v4.2] 血缘输出已停用：不再读取上游阶段血缘（代码保留注释存档）
    # candidates = [input_path] if input_path.is_dir() else [input_path.parent]
    # if not input_path.is_dir():
    #     candidates.append(input_path.parent.parent)
    # history = lineage.load_prior_lineage(candidates)
    # prior_stage = lineage.load_prior_stage_lineage(candidates)
    # return history, prior_stage
    return [], []


# ------------------------------------------------------------------ 媒体搬运
def media_reference(record: Dict[str, Any], name: str) -> Optional[str]:
    """取样本级媒体引用：媒体既可能是相对路径字符串，也可能是含 ``path`` 的对象。"""
    value = record.get(name)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        path = value.get("path")
        return path.strip() if isinstance(path, str) and path.strip() else None
    return None


def media_references(record: Dict[str, Any]) -> List[tuple]:
    """收集一条记录的全部媒体引用 ``[(相对路径, 模态角色), ...]``。

    兼容两种上游形态：样本级 ``text``/``image``/``video``/``audio`` 字段，
    以及文件登记项级 ``files[].path``（#01 `01_workitems.jsonl` 口径）。
    """
    references: List[tuple] = []
    for name in MEDIA_FIELDS:
        reference = media_reference(record, name)
        if reference:
            references.append((reference, name))
    for modality, path in scene.file_references(record):
        references.append((path, modality or "media"))
    return references


def carry_media(records: List[Dict[str, Any]], source_root: Path, run_dir: Path,
                stats: Dict[str, Any]) -> List[Dict[str, str]]:
    """把主产物引用到的媒体按原相对路径原子搬运。

    - 远端链接（http/oss/... ）：只登记不搬运，计入 ``media_remote``（不算错误）；
    - 路径非法（绝对路径/``..``/反斜杠/``~``）：计入 ``media_errors``；
    - 本地相对路径但文件缺失：计入 ``media_missing``；
    - **一律不伪造空文件**。
    """
    artifacts: List[Dict[str, str]] = []
    seen = set()
    for record in records:
        for reference, role in media_references(record):
            if scene.is_remote_reference(reference):
                scene.bump(stats, "media_remote")
                continue
            try:
                source = io_utils.resolve_media(source_root, reference)
            except io_utils.UnsupportedMediaPath:
                scene.bump(stats, "media_errors")
                continue
            if not source.is_file():
                scene.bump(stats, "media_missing")
                continue
            if reference in seen:
                continue
            seen.add(reference)
            target = io_utils.resolve_media(run_dir, reference)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            scene.bump(stats, "media_carried")
            scene.bump(stats, "media_bytes", target.stat().st_size)
            artifacts.append({"path": reference, "role": role})
    return artifacts


# ------------------------------------------------------------------ 主流程
def run(input_file: Optional[str], unique_id: str, *, config_path: Optional[str] = None,
        output_root=None, overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次算子 #02 核验，返回结构化结果（含主产物真实路径与统计）。"""
    if output_root is None:
        output_root = os.environ.get("OUTPUT_DIR") or (OPERATOR_DIR / "output")
    root = io_utils.resolve_root(output_root)
    io_utils.validate_unique_id(unique_id)

    config = load_config(config_path)
    config, overridden_keys = apply_overrides(config, overrides, config_path)
    section = business_config(config)
    stats = scene.initial_statistics()
    stats["enabled"] = bool(config.enabled)

    task = io_utils.task_dir(root, unique_id)
    started_at = now_iso()

    with io_utils.TaskLock(root, unique_id):
        operator_dir = task / config.operator.name
        operator_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        run_log = log_utils.get_logger("operator02", run_dir / "run.log")
        try:
            log_utils.log_action(
                run_log, "start",
                "operator=%s version=%s uniqueId=%s run=%s enabled=%s scene_id_source=%s "
                "min_modalities=%d"
                % (config.operator.name, config.operator.version, unique_id, run_id,
                   str(bool(config.enabled)).lower(), config.scene_id_source,
                   config.min_modalities))
            log_utils.log_action(
                run_log, "capture-readonly",
                "device concurrency/failover are read-only from capture platform; "
                "no device scheduling in this operator")

            result = _execute(run_dir, task, input_file, config, section, stats,
                              log_utils, run_log, allow_external_input, unique_id)

            _verify_writer_counts(stats)
            stats["conservation_ok"] = scene.check_conservation(stats)
            if not stats["conservation_ok"]:
                raise InputError("结果守恒校验失败：%s" % json.dumps(
                    scene.conservation_view(stats), ensure_ascii=False))
            status = scene.resolve_status(stats)
            stats["status"] = status
            scene.log_summary(run_log, stats)
            log_utils.log_action(run_log, "finish",
                                 "status=%s main=%d groups=%d report=%s"
                                 % (status, stats["main_entries"],
                                    stats["scene_groups_total"], REPORT_NAME))

            # 原子提交：先写临时名，全部成功后改名，避免半截文件被下游读到
            for name in OUTPUT_NAMES:
                partial = run_dir / (name + PARTIAL_SUFFIX)
                target = run_dir / name
                if partial.exists():
                    os.replace(str(partial), str(target))
                else:
                    target.write_text("", encoding="utf-8")
            log_utils.close_logger(run_log)

            finished_at = now_iso()
            report = scene.build_report(stats, section, finished_at, unique_id, run_id)
            io_utils.atomic_write_json(run_dir / REPORT_NAME, report)

            result["outputFile"] = str(run_dir / MAIN_NAME)
            result["statistics"] = stats
            result["status"] = status
            result["startedAt"] = started_at
            result["finishedAt"] = finished_at
            result["config"] = config_snapshot(config, config_path, overridden_keys)

            media_artifacts = result.pop("mediaArtifacts")
            artifacts = [{"path": name, "role": scene.ARTIFACT_ROLES[name]}
                         for name in OUTPUT_NAMES]
            artifacts.append({"path": REPORT_NAME, "role": scene.ARTIFACT_ROLES[REPORT_NAME]})
            artifacts.extend(media_artifacts)

            # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录，不再写 lineage.json /
            # stage_context.json（原 make_lineage/append_lineage/build_stage_context 调用块
            # 注释存档）
            # node = lineage.make_lineage(
            #     operator=config.operator.name, version=config.operator.version,
            #     unique_id=unique_id, stage_id=config.stage_id, run_id=run_id,
            #     input_file=result["inputFile"], output_file=str(run_dir / MAIN_NAME),
            #     status=status, started_at=started_at, finished_at=finished_at,
            #     config=result["config"], statistics=stats, artifacts=artifacts,
            #     formal_operator_number=scene.FORMAL_OPERATOR_NUMBER)
            # stage_lineage = lineage.append_lineage(
            #     run_dir / lineage.LINEAGE_NAME, node,
            #     previous=result.pop("priorStageLineage"))
            # prior = result.pop("priorLineage")
            # context = lineage.build_stage_context(
            #     config.stage_id, unique_id, run_id, MAIN_NAME,
            #     lineage.to_stage_lineage(stage_lineage), prior)
            # io_utils.atomic_write_json(run_dir / lineage.STAGE_CONTEXT_NAME, context)

            manifest = transport.build_manifest(MAIN_NAME, artifacts)
            manifest.update({
                "operator": config.operator.name,
                "operator_version": config.operator.version,
                "formal_operator_numbers": [scene.FORMAL_OPERATOR_NUMBER],
                "stage_id": config.stage_id,
                "unique_id": unique_id,
                "run_id": run_id,
                "created_at": finished_at,
                "notes": ("表1 #02：默认关闭（配对集不执行）；融合集打开后核验同一场景编号下"
                          "至少 min_modalities 种模态；无编号且配置为补编号时只绑定一次；"
                          "缺场景编号/模态不足不隔离、改走配对集；"
                          "设备并发与故障切换只读采集平台结果；无模型、无分数"),
            })
            transport.write_manifest(run_dir, manifest)

            result.update({
                "runDir": str(run_dir),
                "runId": run_id,
                "entrypoint": MAIN_NAME,
                "artifacts": artifacts,
                "uniqueId": unique_id,
                "operator": config.operator.name,
                # [v4.2] 血缘输出已停用：stageLineage/localNode/priorLineage 键不再回传
            })
            return result
        except BaseException:
            log_utils.close_logger(run_log)
            for name in list(OUTPUT_NAMES) + [REPORT_NAME]:
                io_utils.cleanup_files(run_dir / name,
                                       run_dir / (name + PARTIAL_SUFFIX))
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
             config: OperatorConfig, section: Dict[str, Any], stats: Dict[str, Any],
             log_module, run_log, allow_external_input: bool,
             unique_id: str) -> Dict[str, Any]:
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

    # [v4.2] 血缘输出已停用：不再读取上游阶段血缘（代码保留注释存档）
    # prior, prior_stage = load_stage_history(source)

    main_path = run_dir / (MAIN_NAME + PARTIAL_SUFFIX)
    groups_path = run_dir / (GROUPS_NAME + PARTIAL_SUFFIX)
    pairing_path = run_dir / (PAIRING_NAME + PARTIAL_SUFFIX)
    origin_path = run_dir / (ORIGIN_NAME + PARTIAL_SUFFIX)
    reject_path = run_dir / (REJECT_NAME + PARTIAL_SUFFIX)

    seen_ids: set = set()
    candidates: List[Dict[str, Any]] = []

    with io_utils.JsonlWriter(origin_path) as origin_writer, \
            io_utils.JsonlWriter(reject_path) as reject_writer:
        for lineno, raw_line in enumerate(
                io_utils.iter_decoded_lines(entrypoint, "utf-8"), start=1):
            scene.bump(stats, "input_lines")
            if not raw_line.strip():
                scene.bump(stats, "blank_lines")
                continue
            scene.bump(stats, "input_records")
            parsed = scene.parse_line(raw_line)
            candidate = scene.scan_record(parsed, lineno, stats, seen_ids, section,
                                          unique_id, now_iso())
            if candidate["reject"]:
                if candidate["route"] == scene.ROUTE_ORIGIN:
                    origin_writer.write(candidate["entry"])
                else:
                    reject_writer.write(candidate["entry"])
            # valid_records：JSON 合法且带 sample_id（含重复样本，重复不计入主产物）
            if candidate["route"] != scene.ROUTE_ORIGIN:
                scene.bump(stats, "valid_records")
            candidates.append(candidate)

    stats["origin_entries"] = origin_writer.count
    stats["reject_entries"] = reject_writer.count
    stats["empty_input"] = stats["input_records"] == 0

    groups = scene.bind_scene_groups(candidates, section, stats)

    main_records: List[Dict[str, Any]] = []
    with io_utils.JsonlWriter(main_path) as main_writer, \
            io_utils.JsonlWriter(groups_path) as group_writer, \
            io_utils.JsonlWriter(pairing_path) as pairing_writer:
        for candidate in candidates:
            if candidate["reject"]:
                continue
            if candidate["route"] not in (scene.ROUTE_FUSION, scene.ROUTE_PAIRING):
                raise InputError("未知处置路由：%s" % candidate["route"])
            record = scene.build_record(candidate, section)
            main_writer.write(record)
            main_records.append(record)
            if candidate["route"] == scene.ROUTE_PAIRING:
                pairing_writer.write(scene.build_pairing_entry(candidate, section))
        for group in groups:
            group_writer.write(scene.build_group_entry(group, section))

    stats["main_entries"] = main_writer.count
    stats["group_entries"] = group_writer.count
    stats["pairing_entries"] = pairing_writer.count

    media_artifacts = carry_media(main_records, entrypoint.parent, run_dir, stats)

    log_module.log_action(run_log, "grouping",
                          "scene_groups=%d verified=%d insufficient=%d"
                          % (stats["scene_groups_total"], stats["verified_groups"],
                             stats["insufficient_groups"]))

    return {"inputFile": str(entrypoint), "mediaArtifacts": media_artifacts}


def _verify_writer_counts(stats: Dict[str, Any]) -> None:
    """用真实产物行数复核路由计数（不只是相信计数器自洽）。"""
    expected_main = stats["fusion_records"] + stats["pairing_records"]
    if stats["main_entries"] != expected_main:
        raise InputError("主产物行数 %d 与路由计数 %d 不一致"
                         % (stats["main_entries"], expected_main))
    if stats["pairing_entries"] != stats["pairing_records"]:
        raise InputError("配对集清单行数 %d 与计数 %d 不一致"
                         % (stats["pairing_entries"], stats["pairing_records"]))
    if stats["origin_entries"] != stats["origin_rejected"]:
        raise InputError("原始隔离清单行数 %d 与计数 %d 不一致"
                         % (stats["origin_entries"], stats["origin_rejected"]))
    if stats["reject_entries"] != stats["duplicate_records"]:
        raise InputError("异常清单行数 %d 与重复计数 %d 不一致"
                         % (stats["reject_entries"], stats["duplicate_records"]))
    if stats["group_entries"] != stats["scene_groups_total"]:
        raise InputError("场景组清单行数 %d 与场景组计数 %d 不一致"
                         % (stats["group_entries"], stats["scene_groups_total"]))


# ------------------------------------------------------------------ 阶段返回形态
# [v4.2] 血缘输出已停用：build_intermediate_lineage/build_stage_payload 不再被
# app.py / CLI 调用（原实现依赖 result["localNode"]/["stageLineage"]，注释存档）
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
    """阶段末最终产物文件内容对象（主产物 + 清单 + 报告 + 被引用媒体）。"""
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
        description="算子 #02 核验场景编号：场景组模态核验 / 补编号只绑定一次 / 改走配对集")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径或素材目录")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识")
    parser.add_argument("--config", default=None, help="部署配置 YAML 路径")
    parser.add_argument("--output-root", default=None, help="输出根目录（默认 $OUTPUT_DIR）")
    parser.add_argument("--override", default=None,
                        help='JSON 字符串，覆盖允许的业务参数'
                             '（如 {"enabled": true, "min_modalities": 2}）')
    parser.add_argument("--is-last-operator", action="store_true",
                        help="按阶段末形态输出（返回 crossmodal-files-v1 文件内容对象）")
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
    except (ConfigError, InputError, io_utils.TaskBusyError,
            transport.TransportError) as exc:
        print(json.dumps({"code": 1, "msg": "%s: %s" % (type(exc).__name__, exc),
                          "data": {}}, ensure_ascii=False))
        return 1
    except ValueError as exc:
        print(json.dumps({"code": 1, "msg": "ValueError: %s" % exc, "data": {}},
                         ensure_ascii=False))
        return 1

    data: Dict[str, Any] = {
        "status": result["status"],
        "operator": result.get("operator"),
        "uniqueId": result.get("uniqueId"),
        "runId": result["runId"],
        "inputFile": result["inputFile"],
        "statistics": result["statistics"],
        "artifacts": result["artifacts"],
    }
    # [v4.2] 阶段返回形态统一：不再区分阶段末/非阶段末，去除 lineageFile；
    # --is-last-operator 仅保留参数兼容，响应始终为 outputFile 文件内容对象
    data["outputFile"] = stage_file_object(result)
    msg = result["status"]
    print(json.dumps({"code": 0, "msg": msg, "data": data}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
