"""算子 #16 标注整体对齐关系：运行编排与 CLI。

一个 uniqueId 一次运行 = 一个 runNNN 目录，产物全部落在
``OUTPUT_DIR/<uniqueId>/16_whole_alignment/runNNN/``，绝不写回上游目录。

CLI：
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
    python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli
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

from typing import Any, Dict, List, Optional

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils import alignment as al  # noqa: E402
from utils import backends, io as op_io, lineage as op_lineage  # noqa: E402
# [v4.2] 血缘输出已停用：op_lineage 仅保留 snapshot（通用文件快照）用途；
# build_lineage/append_lineage/partition_lineage 等血缘调用已全部注释（见 _failure/_execute）。
from utils.logger import Logger  # noqa: E402
from utils.lineage import now_iso  # noqa: E402  # now_iso 只是时间工具，保留使用

OPERATOR = al.OPERATOR
VERSION = al.VERSION
STAGE_ID = al.STAGE_ID
MAIN_NAME = al.MAIN_NAME
REVIEW_NAME = al.REVIEW_NAME
ISOLATION_NAME = al.ISOLATION_NAME
NOT_APPLICABLE_NAME = al.NOT_APPLICABLE_NAME
REJECTS_NAME = al.REJECTS_NAME
AUDIT_NAME = al.AUDIT_NAME
SIDECAR_NAMES = al.SIDECAR_NAMES
DEFAULT_CONFIG = BASE / "configs" / "config.yaml"

#: 人工回写事件文件的自动探测候选（按顺序取第一个存在的）
HUMAN_EVENT_FILES = ("16_human_events.jsonl", "human_events.jsonl", "16_review.jsonl")

# [v4.2] 血缘输出已停用：lineage.json / stage_lineage.json 不再产出；
# 仍列入排除清单，避免历史运行目录中的遗留血缘文件被误打包进 outputFile。
SIDECAR_EXTRA = ("manifest.json", "lineage.json", "stage_lineage.json",
                 "config.snapshot.json")


# ------------------------------------------------------------------ 基础设施

def resolve_output_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get("OUTPUT_DIR")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return (BASE / "output").resolve()


def load_config(config_path: Optional[str] = None) -> dict:
    """读取 YAML 配置；缺失或语法错误都明确报错，不用内置默认值掩盖问题。"""
    target = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG
    if not target.is_file():
        raise al.ConfigError("缺少配置文件：%s" % target)
    try:
        import yaml

        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except al.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise al.ConfigError("配置文件解析失败：%s" % type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise al.ConfigError("配置文件根节点必须是对象")
    return payload


def build_encoder(cfg: dict):
    """按配置宽松构建 CLIP 编码器（测试可替换本函数注入替身）。

    返回 ``(encoder|None, degrade_reason|None)``；``encoder is None`` 表示本机无本地 CLIP，
    此时常规分档仍按 #12 真实分数完成，只有"人工改描述"的记录会显式延后为待人工。
    """
    encoder, reason, _identity = backends.build_clip_backend(cfg, strict=False)
    return encoder, reason


def _degraded_identity(cfg: dict, reason: str) -> dict:
    model_cfg = cfg.get("model", {})
    return {
        "name": backends.CLIP_NAME,
        "version": model_cfg.get("model_name", backends.DEFAULT_ARCH),
        "library": model_cfg.get("library", "auto"),
        "weights": None,
        "device": None,
        "degraded": True,
        "real_backend": False,
        "score_semantics": None,
        "degrade_reason": reason,
    }


def _failure(unique_id: Optional[str], reason: str, started_at: str,
             statistics: Optional[dict] = None, config: Optional[dict] = None) -> dict:
    # [v4.2] 血缘输出已停用：失败路径不再构建血缘节点（代码保留注释存档）。
    # finished = now_iso()
    # node = op_lineage.build_lineage(
    #     OPERATOR, VERSION, unique_id or "", STAGE_ID, "", None, "failed",
    #     started_at, finished, config or {}, statistics or {},
    #     formal_operator_number=al.FORMAL_OPERATOR_NUMBER)
    return {"status": "failed", "reason": reason, "uniqueId": unique_id,
            "operator": OPERATOR, "stageId": STAGE_ID, "statistics": statistics or {},
            "outputFile": None, "reviewFile": None,
            "isolationFile": None, "notApplicableFile": None, "rejectsFile": None,
            "auditFile": None, "manifest": None, "artifacts": [], "runDir": None,
            "runId": None, "config": config or {}}


def resolve_human_events_path(input_dir: Path, explicit: Optional[str]) -> Optional[Path]:
    """解析人工回写事件文件：显式路径优先，否则在素材目录内按候选名探测。"""
    if explicit:
        target = Path(explicit).expanduser()
        return target if target.is_absolute() else (Path.cwd() / target).resolve()
    for name in HUMAN_EVENT_FILES:
        candidate = Path(input_dir) / name
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------ 主流程

def rescore_log_evidence(result):
    """合并所有台账，按配对去重，保持每次真实重评分只记录一次。"""
    rescored_pairs = []
    seen_pairs = set()
    # 重评分后的样本可能被判进隔离档，因此要遍历全部台账而不只是主清单；
    # 旁路台账（隔离/人工/不适用）是「台账条目」形状，配对记录嵌在 record 下。
    for item in list(result.main) + list(result.isolation) + list(result.review) \
            + list(result.not_applicable):
        whole = ((item.get("alignment") or {}).get("whole") or {})
        if not whole:
            whole = (((item.get("record") or {}).get("alignment") or {}).get("whole") or {})
        pair_id = item.get("pair_id") or (item.get("record") or {}).get("pair_id")
        if whole.get("bucketed_by") == al.CONFIRM_RESCORE and pair_id not in seen_pairs:
            seen_pairs.add(pair_id)
            edit = whole.get("description_edit") or {}
            rescored_pairs.append((pair_id, edit.get("score_before"),
                                   whole.get("clip_score"),
                                   whole.get("decision")))
    return rescored_pairs


def run(input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None,
        run_id: Optional[str] = None, encoder: Optional[Any] = None,
        human_events_path: Optional[str] = None) -> Dict[str, Any]:
    """执行一次整体对齐标注；返回 status/statistics/lineage 与全部产物路径。

    ``encoder=None`` 表示按配置加载本地 CLIP；加载不到时只影响"人工改描述重评分"，
    此时如实延后为待人工或按 ``allow_degraded`` 失败，**不会伪造任何分数**。
    """
    started_at = now_iso()

    # 1) uniqueId 与任务目录安全
    try:
        safe_id = op_io.validate_unique_id(unique_id)
    except ValueError as exc:
        return _failure(unique_id, "invalid_unique_id: %s" % exc, started_at)

    # 2) 输入文件
    source = Path(input_file).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    else:
        source = source.resolve()
    if not source.is_file():
        return _failure(safe_id, "input_not_found", started_at)

    # 3) 配置（含请求级业务覆盖）—— 先校验，避免半个坏配置进入运行
    try:
        cfg = al.validate_config(al.apply_overrides(load_config(config_path), overrides))
    except al.ConfigError as exc:
        return _failure(safe_id, "config_error: %s" % exc, started_at)
    except Exception as exc:  # noqa: BLE001
        return _failure(safe_id, "config_error: %s" % type(exc).__name__, started_at)

    # 4) 人工回写事件文件（缺失即视为本批无人工结论，不是错误）
    events_file = resolve_human_events_path(source.parent, human_events_path)
    if events_file is not None and not events_file.is_file():
        return _failure(safe_id, "human_events_not_found", started_at, config=cfg)

    # 5) 本地 CLIP：宽松构建，不可用时只影响重评分
    model_reason: Optional[str] = None
    model_identity: Optional[dict] = None
    if encoder is None:
        encoder, reason = build_encoder(cfg)
        if encoder is None:
            model_reason = reason or al.REASON_MODEL_UNAVAILABLE
            model_identity = _degraded_identity(cfg, model_reason)
        else:
            model_identity = encoder.model_identity()
    else:
        model_identity = encoder.model_identity()

    media_root = source.parent
    output_root_path = resolve_output_root(output_root)

    try:
        with op_io.task_lock(output_root_path, safe_id):
            return _execute(source, safe_id, cfg, encoder, model_identity, model_reason,
                            events_file, media_root, output_root_path, started_at, run_id)
    except op_io.TaskBusy as exc:
        return _failure(safe_id, str(exc), started_at, config=cfg)


def _execute(source: Path, safe_id: str, cfg: dict, encoder: Optional[Any],
             model_identity: Optional[dict], model_reason: Optional[str],
             events_file: Optional[Path], media_root: Path, output_root: Path,
             started_at: str, run_id: Optional[str]) -> Dict[str, Any]:
    task = op_io.task_dir(output_root, safe_id)
    operator_root = task / OPERATOR
    explicit_run = run_id is not None
    run_id = run_id or op_io.next_run_id(operator_root)
    if not isinstance(run_id, str) or not run_id.startswith("run"):
        return _failure(safe_id, "invalid_run_id", started_at, config=cfg)
    run_dir = operator_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=explicit_run)
    except FileExistsError:
        return _failure(safe_id, "run_dir_exists", started_at, config=cfg)
    except OSError as exc:
        return _failure(safe_id, "run_dir_unwritable: %s" % type(exc).__name__,
                        started_at, config=cfg)

    planned = {name: run_dir / name
               for name in (MAIN_NAME, REVIEW_NAME, ISOLATION_NAME, NOT_APPLICABLE_NAME,
                            REJECTS_NAME, AUDIT_NAME)}
    if any(path.resolve() == source.resolve() for path in planned.values()):
        return _failure(safe_id, "output_conflicts_with_input", started_at, config=cfg)

    log = Logger(run_dir / "run.log")
    log("start", "operator=%s version=%s uniqueId=%s runId=%s input=%s"
        % (OPERATOR, VERSION, safe_id, run_id, source.name))
    log("input", "路径=%s" % source)
    if events_file is not None:
        log("human", "已载入人工回写事件文件=%s" % events_file.name)
    else:
        log("human", "本批未提供人工回写事件文件")
    if encoder is None:
        log("model", "本地 CLIP 不可用（%s）：常规分档仍用 #12 真实分数，"
                     "人工改描述记录将延后为待人工" % model_reason)
    else:
        log("model", "后端=%s device=%s（用于人工改描述重评分）"
            % (model_identity.get("library"), model_identity.get("device")))

    events = None
    if events_file is not None:
        events = al.load_human_events(op_io.read_jsonl(events_file))

    try:
        result = al.plan_alignment(
            op_io.read_jsonl(source), cfg, unique_id=safe_id, media_root=media_root,
            now=started_at, encoder=encoder, events=events, model_reason=model_reason)
    except al.RescoreUnavailable as exc:
        log("failed", "重评分不可用：%s" % exc)
        return _failure(safe_id, "rescore_requires_model: %s" % exc, started_at, config=cfg)

    log("aligned", "输入=%d 自动对齐=%d 人工对齐=%d 待人工=%d 存疑=%d 不对齐=%d 不适用=%d"
        % (result.stats["input"], result.stats["auto_aligned"],
           result.stats["manual_aligned"], result.stats["pending_manual_review"],
           result.stats["uncertain"], result.stats["not_aligned"],
           result.stats["not_applicable"]))

    # 表1 #16 算法 4) 完成判据：人补写/改写描述后，日志中必须有**一次**重新评分记录
    rescored_pairs = rescore_log_evidence(result)
    for pair_id, before, after, decision in rescored_pairs:
        log("rescore", "pair_id=%s 人工改写描述后重新评分一次："
                       "score_before=%s score_after=%s 结论=%s"
            % (pair_id, before, after, decision))
    if rescored_pairs:
        log("rescore_summary", "人改描述重评分 %d 次（每个样本恰好一次，档位由新分给出）"
            % len(rescored_pairs))

    counts = {
        MAIN_NAME: op_io.atomic_write_jsonl(planned[MAIN_NAME], result.main,
                                            source_path=source),
        REVIEW_NAME: op_io.atomic_write_jsonl(planned[REVIEW_NAME], result.review,
                                              source_path=source),
        ISOLATION_NAME: op_io.atomic_write_jsonl(planned[ISOLATION_NAME], result.isolation,
                                                 source_path=source),
        NOT_APPLICABLE_NAME: op_io.atomic_write_jsonl(
            planned[NOT_APPLICABLE_NAME], result.not_applicable, source_path=source),
        REJECTS_NAME: op_io.atomic_write_jsonl(planned[REJECTS_NAME], result.rejects,
                                               source_path=source),
        AUDIT_NAME: op_io.atomic_write_jsonl(planned[AUDIT_NAME], result.audit,
                                             source_path=source),
    }

    # 媒体携带：把本次记录真实引用的图像按相对路径复制进运行目录，便于阶段末打包
    referenced = list(result.stats.get("_media_refs") or [])
    carried, missing = op_io.copy_media(run_dir, media_root, referenced)
    if missing:
        log("media", "缺失媒体 %d 个（已在异常清单登记）：%s"
            % (len(missing), ",".join(missing[:5])))

    statistics = _public_statistics(result.stats, counts, cfg, model_identity, carried)
    status = "degraded" if statistics["degraded"] else "success"

    artifacts: List[dict] = [
        {"path": MAIN_NAME, "role": "main", "records": counts[MAIN_NAME]},
        {"path": REVIEW_NAME, "role": "review", "records": counts[REVIEW_NAME]},
        {"path": ISOLATION_NAME, "role": "review", "records": counts[ISOLATION_NAME]},
        {"path": NOT_APPLICABLE_NAME, "role": "review", "records": counts[NOT_APPLICABLE_NAME]},
        {"path": REJECTS_NAME, "role": "review", "records": counts[REJECTS_NAME]},
        {"path": AUDIT_NAME, "role": "review", "records": counts[AUDIT_NAME]},
    ]
    manifest = {
        "format": "crossmodal-manifest-v1",
        "operator": OPERATOR,
        "operator_version": VERSION,
        "formalOperatorNumber": al.FORMAL_OPERATOR_NUMBER,
        "stageId": STAGE_ID,
        "uniqueId": safe_id,
        "runId": run_id,
        "entrypoint": MAIN_NAME,
        "status": status,
        "artifacts": artifacts,
        "media": {"carried": carried, "missing": missing},
        "counts": counts,
        "statistics": statistics,
        "humanEventsFile": events_file.name if events_file else None,
        "producedAt": now_iso(),
    }
    op_lineage.snapshot(run_dir, "manifest.json", manifest)

    config_snapshot = op_lineage.snapshot(run_dir, "config.snapshot.json", cfg)
    # [v4.2] 血缘/阶段归档输出已停用：不再构建/追加/快照血缘节点与阶段血缘
    # （代码保留注释存档，不删除）。
    # finished_at = now_iso()
    # node = op_lineage.build_lineage(
    #     OPERATOR, VERSION, safe_id, STAGE_ID, str(source), str(planned[MAIN_NAME]),
    #     status, started_at, finished_at, cfg, statistics, run_id=run_id,
    #     model=model_identity,
    #     artifacts=[item["path"] for item in artifacts] + [config_snapshot.name, "run.log"],
    #     formal_operator_number=al.FORMAL_OPERATOR_NUMBER)
    #
    # op_lineage.append_lineage(task, node)
    # op_lineage.snapshot(run_dir, "lineage.json", node)
    # stage_nodes, prior_nodes = op_lineage.partition_lineage(
    #     op_lineage.load_lineage_history(task), STAGE_ID, safe_id)
    # stage_lineage = [op_lineage.public_node(item) for item in stage_nodes]
    # op_lineage.snapshot(run_dir, "stage_lineage.json", stage_lineage)

    manifest["artifacts"].extend([
        {"path": "manifest.json", "role": "sidecar"},
        {"path": config_snapshot.name, "role": "sidecar"},
        # [v4.2] 血缘文件已停用：不再登记 lineage.json / stage_lineage.json 旁车
        {"path": "run.log", "role": "sidecar"},
    ])
    if carried:
        manifest["artifacts"].append({"path": "media", "role": "media",
                                      "files": len(carried)})
    op_lineage.snapshot(run_dir, "manifest.json", manifest)
    publish_stage_output(run_dir)

    log("finish", "status=%s 主清单=%d 人工队列=%d 隔离=%d 不适用=%d 异常=%d 审计=%d"
        % (status, counts[MAIN_NAME], counts[REVIEW_NAME], counts[ISOLATION_NAME],
           counts[NOT_APPLICABLE_NAME], counts[REJECTS_NAME], counts[AUDIT_NAME]))

    return {
        "status": status,
        "reason": statistics.get("degradation_reason"),
        "uniqueId": safe_id,
        "operator": OPERATOR,
        "stageId": STAGE_ID,
        "runId": run_id,
        "runDir": str(run_dir),
        "outputFile": str(planned[MAIN_NAME]),
        "reviewFile": str(planned[REVIEW_NAME]),
        "isolationFile": str(planned[ISOLATION_NAME]),
        "notApplicableFile": str(planned[NOT_APPLICABLE_NAME]),
        "rejectsFile": str(planned[REJECTS_NAME]),
        "auditFile": str(planned[AUDIT_NAME]),
        "artifacts": [item["path"] for item in manifest["artifacts"]],
        "manifest": manifest,
        "statistics": statistics,
        # [v4.2] 血缘输出已停用：run() 不再回传 lineage/stageLineage/priorLineage 键
        "config": cfg,
        "model": model_identity,
    }


def _public_statistics(stats: dict, counts: Dict[str, int], cfg: dict,
                       model_identity: Optional[dict],
                       carried: Optional[List[str]] = None) -> dict:
    """对外统计：内部真实计数 + 阈值/模型/守恒，全部来自真实结果。"""
    public = {key: value for key, value in stats.items() if not key.startswith("_")}
    public.update({
        "media_carried": len(carried or []),
        "output": counts[MAIN_NAME],
        "rejected": counts[REJECTS_NAME],
        "review": counts[REVIEW_NAME],
        "isolated": counts[ISOLATION_NAME],
        "not_applicable": counts[NOT_APPLICABLE_NAME],
        "audit": counts[AUDIT_NAME],
        "thresholds": {"isolation": cfg[al.SECTION]["isolation_threshold"],
                       "auto_align": cfg[al.SECTION]["auto_align_threshold"]},
        "route_enabled": cfg[al.SECTION]["enabled"],
        "model": model_identity,
        "operator": OPERATOR,
        "operator_version": VERSION,
        "formal_operator_number": al.FORMAL_OPERATOR_NUMBER,
    })
    return public


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 #16 标注整体对齐关系：整体—整体对齐结论、人工三按钮与否决锁定")
    parser.add_argument("--input", required=True, help="输入候选对 JSONL 路径")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识（单层安全目录名）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 configs/config.yaml）")
    parser.add_argument("--output-root", default=None, help="输出根目录（默认 OUTPUT_DIR 或 output/）")
    parser.add_argument("--run-id", default=None, help="指定运行目录名（默认自动 runNNN）")
    parser.add_argument("--human-events", default=None,
                        help="人工回写事件 JSONL（默认在素材目录自动探测）")
    parser.add_argument("--isolation-threshold", type=float, default=None,
                        help="不对齐阈值（0.20–0.40，默认 0.20）")
    parser.add_argument("--auto-align-threshold", type=float, default=None,
                        help="自动对齐阈值（0.25–0.50，默认 0.32）")
    parser.add_argument("--is-last-operator", action="store_true",
                        help="按阶段末形态打印（真实文件对象由 HTTP 接口返回）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.isolation_threshold is not None:
        overrides["isolation_threshold"] = args.isolation_threshold
    if args.auto_align_threshold is not None:
        overrides["auto_align_threshold"] = args.auto_align_threshold

    result = run(args.input, args.unique_id, config_path=args.config,
                 output_root=args.output_root, overrides=overrides or None,
                 run_id=args.run_id, human_events_path=args.human_events)
    statistics = result.get("statistics", {})
    # [v4.2] 形态二响应不再区分阶段末/非阶段末：统一 outputFile 文件对象（去 lineageFile）
    data_shape = {"outputFile": "file-object"}
    entry = (result.get("manifest") or {}).get("entrypoint")
    report = {
        "status": result["status"],
        "reason": result.get("reason"),
        "uniqueId": result.get("uniqueId"),
        "runDir": result.get("runDir"),
        "outputFile": result.get("outputFile"),
        "entrypoint": entry,
        "dataShape": data_shape,
        "statistics": {key: value for key, value in statistics.items()
                       if key not in {"model", "thresholds"}},
        "thresholds": statistics.get("thresholds"),
        "model": statistics.get("model"),
        # [v4.2] 血缘输出已停用：CLI 报告不再附带 lineage 节点
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"success", "degraded"} else 1


if __name__ == "__main__":
    sys.exit(main())
