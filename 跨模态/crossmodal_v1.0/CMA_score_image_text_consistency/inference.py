"""算子 #12 评分图文语义一致性：运行编排与 CLI。

一个 uniqueId 一次运行 = 一个 runNNN 目录，产物全部落在
``OUTPUT_DIR/<uniqueId>/image_text_semantic_scoring/runNNN/``，绝不写回上游目录。

CLI：
    python processor.py --input dataset/sample/pairs.jsonl --unique-id smoke_cli --output-root output_smoke
    python inference.py --input dataset/sample/pairs.jsonl --unique-id smoke_cli
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

# [v4.2] 血缘输出已停用：op_lineage 仅保留 snapshot（manifest/config 快照落盘）与 now_iso 的调用，
# build_lineage / append_lineage / partition_lineage / load_lineage_history / public_node
# 的调用点已全部注释，代码保留存档。
from utils import backends, io as op_io, lineage as op_lineage, scoring  # noqa: E402
from utils.logger import Logger  # noqa: E402
from utils.lineage import now_iso  # noqa: E402

OPERATOR = scoring.OPERATOR
VERSION = scoring.VERSION
STAGE_ID = scoring.STAGE_ID
MAIN_NAME = scoring.MAIN_NAME
FLOW_NAME = scoring.FLOW_NAME
ISOLATION_NAME = scoring.ISOLATION_NAME
NOT_APPLICABLE_NAME = scoring.NOT_APPLICABLE_NAME
PENDING_MODEL_NAME = scoring.PENDING_MODEL_NAME
REJECTS_NAME = scoring.REJECTS_NAME
SIDECAR_NAMES = (ISOLATION_NAME, NOT_APPLICABLE_NAME, PENDING_MODEL_NAME, REJECTS_NAME)
DEFAULT_CONFIG = BASE / "configs" / "config.yaml"


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
        raise scoring.ConfigError("缺少配置文件：%s" % target)
    try:
        import yaml

        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except scoring.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise scoring.ConfigError("配置文件解析失败：%s" % type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise scoring.ConfigError("配置文件根节点必须是对象")
    return payload


def build_encoder(cfg: dict):
    """按配置构建 CLIP 编码器（测试可替换本函数注入替身）。"""
    return backends.build_clip_backend(cfg)


def build_detector(cfg: dict):
    """按配置构建可选 YOLOv8 辅助检测（测试可替换本函数注入替身）。"""
    return backends.build_detector(cfg)


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
    # [v4.2] 血缘输出已停用：失败不再构建血缘节点，代码保留存档备查。
    # finished = now_iso()
    # node = op_lineage.build_lineage(
    #     OPERATOR, VERSION, unique_id or "", STAGE_ID, "", None, "failed",
    #     started_at, finished, config or {}, statistics or {},
    #     formal_operator_number=scoring.FORMAL_OPERATOR_NUMBER)
    return {"status": "failed", "reason": reason, "uniqueId": unique_id,
            "operator": OPERATOR, "stageId": STAGE_ID, "statistics": statistics or {},
            "outputFile": None, "isolationFile": None, "pendingModelFile": None,
            "notApplicableFile": None, "rejectsFile": None, "manifest": None,
            "artifacts": [], "runDir": None, "runId": None, "config": config or {}}


# ------------------------------------------------------------------ 主流程

def run(input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None,
        run_id: Optional[str] = None, encoder: Optional[Any] = None,
        detector: Optional[Any] = None) -> Dict[str, Any]:
    """执行一次评分；返回 status/statistics/lineage 与全部产物路径。

    ``encoder=None`` 表示按配置加载真实后端；加载不到时按 ``model.allow_degraded``
    输出 degraded（待模型清单）或 failed，**不会伪造分数**。
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
        cfg = scoring.validate_config(
            scoring.apply_overrides(load_config(config_path), overrides))
    except scoring.ConfigError as exc:
        return _failure(safe_id, "config_error: %s" % exc, started_at)
    except Exception as exc:  # noqa: BLE001
        return _failure(safe_id, "config_error: %s" % type(exc).__name__, started_at)

    # 4) 后端：真实模型优先，不可用时明确表态
    degrade_reason: Optional[str] = None
    degrade_detail: Optional[str] = None
    if encoder is None:
        try:
            encoder, degrade_reason, identity = build_encoder(cfg)
        except backends.BackendUnavailable as exc:
            return _failure(safe_id, "model_unavailable: %s" % exc, started_at, config=cfg)
        if encoder is None:
            degrade_detail = degrade_reason or scoring.REASON_MODEL_UNAVAILABLE
            degrade_reason = scoring.REASON_MODEL_UNAVAILABLE
            model_identity = _degraded_identity(cfg, degrade_detail)
        else:
            model_identity = identity or encoder.model_identity()
    else:
        model_identity = encoder.model_identity()

    detector_reason: Optional[str] = None
    if detector is None and cfg["image_text_scoring"]["use_detector_for_borderline"]:
        detector, detector_reason = build_detector(cfg)

    media_root = source.parent
    output_root_path = resolve_output_root(output_root)

    try:
        with op_io.task_lock(output_root_path, safe_id):
            return _execute(source, safe_id, cfg, encoder, detector, detector_reason,
                            degrade_reason, degrade_detail, model_identity, media_root,
                            output_root_path, started_at, run_id)
    except op_io.TaskBusy as exc:
        return _failure(safe_id, str(exc), started_at, config=cfg)


def _execute(source: Path, safe_id: str, cfg: dict, encoder: Optional[Any],
             detector: Optional[Any], detector_reason: Optional[str],
             degrade_reason: Optional[str], degrade_detail: Optional[str],
             model_identity: dict, media_root: Path, output_root: Path,
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

    planned = {name: run_dir / name for name in (MAIN_NAME, FLOW_NAME, *SIDECAR_NAMES)}
    if any(path.resolve() == source.resolve() for path in planned.values()):
        return _failure(safe_id, "output_conflicts_with_input", started_at, config=cfg)

    log = Logger(run_dir / "run.log")
    log("start", "operator=%s version=%s uniqueId=%s runId=%s input=%s"
        % (OPERATOR, VERSION, safe_id, run_id, source.name))
    log("input", "路径=%s" % source)
    if encoder is None:
        log("model", "真实后端不可用，按降级处理：%s" % (degrade_detail or degrade_reason))
    else:
        log("model", "后端=%s device=%s" % (model_identity.get("library"),
                                            model_identity.get("device")))

    result = scoring.plan_scoring(
        op_io.read_jsonl(source), cfg, unique_id=safe_id, media_root=media_root,
        now=started_at, encoder=encoder, detector=detector,
        detector_reason=detector_reason, degraded_reason=degrade_reason,
        degraded_detail=degrade_detail)
    log("scored", "配对=%d 图文对=%d 自动对齐=%d 人工=%d 隔离=%d 待模型=%d"
        % (result.stats["pairs_total"], result.stats["image_text_pairs"],
           result.stats["auto_aligned"], result.stats["pending_manual"],
           result.stats["isolated"], result.stats["pending_model"]))

    counts = {
        FLOW_NAME: op_io.atomic_write_jsonl(planned[FLOW_NAME], result.pipeline, source_path=source),
        MAIN_NAME: op_io.atomic_write_jsonl(planned[MAIN_NAME], result.main,
                                            source_path=source),
        ISOLATION_NAME: op_io.atomic_write_jsonl(planned[ISOLATION_NAME], result.isolation,
                                                 source_path=source),
        NOT_APPLICABLE_NAME: op_io.atomic_write_jsonl(
            planned[NOT_APPLICABLE_NAME], result.not_applicable, source_path=source),
        PENDING_MODEL_NAME: op_io.atomic_write_jsonl(
            planned[PENDING_MODEL_NAME], result.pending_model, source_path=source),
        REJECTS_NAME: op_io.atomic_write_jsonl(planned[REJECTS_NAME], result.rejects,
                                               source_path=source),
    }

    # 媒体携带：把被引用的上游媒体按相对路径复制到本次运行目录，供阶段末打包
    referenced = _referenced_media(result)
    carried, missing = op_io.copy_media(run_dir, media_root, referenced)
    if missing:
        log("media", "缺失媒体 %d 个（已在异常清单登记）：%s"
            % (len(missing), ",".join(missing[:5])))

    statistics = _public_statistics(result.stats, counts, cfg, model_identity)
    status = "degraded" if statistics["degraded"] else "success"

    artifacts: List[dict] = [
        {"path": FLOW_NAME, "role": "pipeline", "records": counts[FLOW_NAME]},
        {"path": MAIN_NAME, "role": "main", "records": counts[MAIN_NAME]},
        {"path": ISOLATION_NAME, "role": "review", "records": counts[ISOLATION_NAME]},
        {"path": NOT_APPLICABLE_NAME, "role": "review",
         "records": counts[NOT_APPLICABLE_NAME]},
        {"path": PENDING_MODEL_NAME, "role": "review", "records": counts[PENDING_MODEL_NAME]},
        {"path": REJECTS_NAME, "role": "review", "records": counts[REJECTS_NAME]},
    ]
    manifest = {
        "format": "crossmodal-manifest-v1",
        "operator": OPERATOR,
        "operator_version": VERSION,
        "formalOperatorNumber": scoring.FORMAL_OPERATOR_NUMBER,
        "stageId": STAGE_ID,
        "uniqueId": safe_id,
        "runId": run_id,
        "entrypoint": MAIN_NAME,
        "pipeline_entrypoint": FLOW_NAME,
        "business_entrypoint": MAIN_NAME,
        "status": status,
        "artifacts": artifacts,
        "media": {"carried": carried, "missing": missing},
        "counts": counts,
        "statistics": statistics,
        "producedAt": now_iso(),
    }
    op_lineage.snapshot(run_dir, "manifest.json", manifest)

    config_snapshot = op_lineage.snapshot(run_dir, "config.snapshot.json", cfg)
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘节点、不追加任务血缘历史、
    # 不再落盘 lineage.json / stage_lineage.json；代码保留存档备查。
    # finished_at = now_iso()
    # node = op_lineage.build_lineage(
    #     OPERATOR, VERSION, safe_id, STAGE_ID, str(source), str(planned[MAIN_NAME]),
    #     status, started_at, finished_at, cfg, statistics, run_id=run_id,
    #     model=model_identity,
    #     artifacts=[item["path"] for item in artifacts] + [config_snapshot.name, "run.log"],
    #     formal_operator_number=scoring.FORMAL_OPERATOR_NUMBER)
    #
    # op_lineage.append_lineage(task, node)
    # op_lineage.snapshot(run_dir, "lineage.json", node)
    # stage_nodes, prior_nodes = op_lineage.partition_lineage(
    #     op_lineage.load_lineage_history(task), STAGE_ID, safe_id)
    # stage_lineage = [op_lineage.public_node(item) for item in stage_nodes]
    # op_lineage.snapshot(run_dir, "stage_lineage.json", stage_lineage)

    if manifest["artifacts"] is not None:
        manifest["artifacts"].extend([
            {"path": "manifest.json", "role": "sidecar"},
            {"path": config_snapshot.name, "role": "sidecar"},
            # [v4.2] 血缘停用：产物清单不再登记 lineage.json / stage_lineage.json
            {"path": "run.log", "role": "sidecar"},
        ])
        if carried:
            manifest["artifacts"].append({"path": "media", "role": "media",
                                          "files": len(carried)})
        op_lineage.snapshot(run_dir, "manifest.json", manifest)
        publish_stage_output(run_dir)

    log("finish", "status=%s 输出=%d 隔离=%d 待模型=%d 原始隔离=%d 不适用=%d"
        % (status, counts[MAIN_NAME], counts[ISOLATION_NAME], counts[PENDING_MODEL_NAME],
           counts[REJECTS_NAME], counts[NOT_APPLICABLE_NAME]))

    return {
        "status": status,
        "reason": statistics.get("degradation_reason"),
        "uniqueId": safe_id,
        "operator": OPERATOR,
        "stageId": STAGE_ID,
        "runId": run_id,
        "runDir": str(run_dir),
        "outputFile": str(planned[MAIN_NAME]),
        "flowEntrypoint": FLOW_NAME,
        "isolationFile": str(planned[ISOLATION_NAME]),
        "notApplicableFile": str(planned[NOT_APPLICABLE_NAME]),
        "pendingModelFile": str(planned[PENDING_MODEL_NAME]),
        "rejectsFile": str(planned[REJECTS_NAME]),
        "artifacts": [item["path"] for item in manifest["artifacts"]],
        "manifest": manifest,
        "statistics": statistics,
        # [v4.2] 血缘停用：返回值不再携带 "lineage" / "stageLineage" / "priorLineage" 键。
        "config": cfg,
        "model": model_identity,
    }


def _referenced_media(result: Any) -> List[str]:
    """Carry every route's native media, including records not applicable to image scoring."""
    from utils.form1_package import active_media_refs
    referenced = set()
    for item in list(result.pipeline) + list(result.isolation) + list(result.rejects):
        referenced.update(active_media_refs(item))
    return sorted(referenced)


def _public_statistics(stats: dict, counts: Dict[str, int], cfg: dict,
                       model_identity: dict) -> dict:
    """对外统计：内部真实计数 + 阈值/模型/守恒，全部来自真实结果。"""
    public = dict(stats)
    public.update({
        "output": counts[MAIN_NAME],
        "rejected": counts[REJECTS_NAME],
        "not_applicable": counts[NOT_APPLICABLE_NAME],
        "thresholds": {"isolation": cfg["image_text_scoring"]["isolation_threshold"],
                       "auto_align": cfg["image_text_scoring"]["auto_align_threshold"]},
        "route_enabled": cfg["image_text_scoring"]["enabled"],
        "detector_requested": cfg["image_text_scoring"]["use_detector_for_borderline"],
        "model": model_identity,
        "operator": OPERATOR,
        "operator_version": VERSION,
        "formal_operator_number": scoring.FORMAL_OPERATOR_NUMBER,
    })
    return public


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 #12 评分图文语义一致性：CLIP-ViT/B-32 真实余弦评分与分档")
    parser.add_argument("--input", required=True, help="输入候选对 JSONL 路径")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识（单层安全目录名）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 configs/config.yaml）")
    parser.add_argument("--output-root", default=None, help="输出根目录（默认 OUTPUT_DIR 或 output/）")
    parser.add_argument("--run-id", default=None, help="指定运行目录名（默认自动 runNNN）")
    parser.add_argument("--isolation-threshold", type=float, default=None,
                        help="低分隔离阈值（0.20–0.40，默认 0.20）")
    parser.add_argument("--auto-align-threshold", type=float, default=None,
                        help="自动对齐阈值（0.25–0.50，默认 0.32）")
    parser.add_argument("--enable-detector", action="store_true",
                        help="打开 YOLOv8 辅助（只补证据，不改变分档）")
    parser.add_argument("--disable-route", action="store_true", help="关闭 #12 图文评分路线")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.isolation_threshold is not None:
        overrides["isolation_threshold"] = args.isolation_threshold
    if args.auto_align_threshold is not None:
        overrides["auto_align_threshold"] = args.auto_align_threshold
    if args.enable_detector:
        overrides["use_detector_for_borderline"] = True
    if args.disable_route:
        overrides["enabled"] = False

    result = run(args.input, args.unique_id, config_path=args.config,
                 output_root=args.output_root, overrides=overrides or None,
                 run_id=args.run_id)
    report = {
        "status": result["status"],
        "reason": result.get("reason"),
        "uniqueId": result.get("uniqueId"),
        "runDir": result.get("runDir"),
        "outputFile": result.get("outputFile"),
        "statistics": {key: value for key, value in result.get("statistics", {}).items()
                       if key != "model"},
        "model": result.get("statistics", {}).get("model"),
        # [v4.2] 血缘停用：CLI 报告不再携带 lineage 节点
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"success", "degraded"} else 1


if __name__ == "__main__":
    sys.exit(main())
