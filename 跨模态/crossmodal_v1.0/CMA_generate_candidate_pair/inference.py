"""算子 #11 生成候选配对：业务入口 run()、CLI 与产物落盘。

职责边界（表1）：只按「同来源编号/场景 → 时间重叠 → 共同关键词」三级规则生成候选配对与 A/B/C 证据，
一对多拆成一对一并共享 group_id；配不出候选的记录留在未配对清单，不隔离、不进入评分。
本算子不加载 CLIP、不算相似度，也不生成任何模型分数。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
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

from typing import Dict, List, Optional, Sequence

import yaml

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils import io as op_io  # noqa: E402
# [v4.2] 血缘输出已停用：仅保留 now_iso（时间工具）与 snapshot（manifest 落盘）的调用，
# build_lineage / append_lineage / partition_lineage / load_lineage_history 的调用点已全部注释，代码保留存档。
from utils import lineage as lineage_util  # noqa: E402
from utils.logger import Logger  # noqa: E402
from utils.pairing import (MODALITY_ORDER, ConfigError, apply_overrides, is_present,  # noqa: E402
                           plan_pairs, validate_config)

OPERATOR = "generate_candidate_pairs"
VERSION = "v1.0.0"
STAGE_ID = "S3"
STAGE_NAME = "跨模态数据对齐标注（表1 数据加工）"
MAIN_NAME = "11_candidate_pairs.jsonl"
UNPAIRED_NAME = "11_unpaired.jsonl"
REJECTS_NAME = "11_rejects.jsonl"
LEDGER_STAGE = "11"

# 拒收原因 → 表1 台账标签/动作/去向
_LEDGER = {
    "json_parse_error": ("原始文件-无法解析", "记录并隔离", "原始隔离区"),
    "record_not_object": ("原始文件-无法解析", "记录并隔离", "原始隔离区"),
    "encoding_error": ("原始文件-编码异常", "记录并隔离", "原始隔离区"),
    "missing_field: sample_id": ("原始文件-缺字段", "记录并隔离", "原始隔离区"),
    "no_modality": ("流程-无有效模态", "不配对并记录", "算子异常清单（不隔离）"),
    "duplicate_sample_id": ("流程-重复样本ID", "跳过重复记录", "算子异常清单（不隔离）"),
    "not_admitted": ("流程-未准入", "不配对并记录", "算子异常清单（不隔离）"),
    "group_pair_limit_exceeded": ("流程-配对上限", "记录并保留未配对", "算子异常清单（不隔离）"),
}
LEDGER_STAGE_LABEL = LEDGER_STAGE


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    with open(str(path), "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ConfigError("配置文件必须是对象：%s" % path)
    return payload


def _ledger(line: int, sample_id: Optional[str], reason: str, record: Optional[dict] = None,
            extra: Optional[dict] = None) -> dict:
    label, action, destination = _LEDGER.get(reason, ("流程-其他异常", "记录", "算子异常清单（不隔离）"))
    item = {
        "line": line,
        "sample_id": sample_id,
        "reason": reason,
        "label": label,
        "stage": LEDGER_STAGE_LABEL,
        "operator": OPERATOR,
        "action": action,
        "destination": destination,
        "event_time": lineage_util.now_iso(),
        "batch_id": (record or {}).get("batch_id") if isinstance(record, dict) else None,
        "retry_count": 0,
    }
    if extra:
        item.update(extra)
    if record is not None:
        item["record"] = record
    return item


def _collect_inputs(input_path: Path, seen: Dict[str, int]):
    """流式读取输入，产出 (有效记录, 有效行号, 异常清单, 坏行数, 读入总数)。"""
    records: List[dict] = []
    lines: List[int] = []
    rejects: List[dict] = []
    bad_lines = 0
    total = 0
    for lineno, record, error in op_io.read_jsonl(input_path):
        total += 1
        if error is not None:
            bad_lines += 1
            rejects.append(_ledger(lineno, None, error))
            continue
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            rejects.append(_ledger(lineno, None, "missing_field: sample_id", record=record))
            continue
        sample_id = sample_id.strip()
        if not any(is_present(record.get(key)) for key in MODALITY_ORDER):
            rejects.append(_ledger(lineno, sample_id, "no_modality", record=record))
            continue
        admission = record.get("admission")
        if isinstance(admission, dict) and admission.get("passed") is False:
            rejects.append(_ledger(lineno, sample_id, "not_admitted", record=record,
                                   extra={"admission": admission}))
            continue
        if sample_id in seen:
            rejects.append(_ledger(lineno, sample_id, "duplicate_sample_id", record=record,
                                   extra={"first_line": seen[sample_id]}))
            continue
        seen[sample_id] = lineno
        records.append(record)
        lines.append(lineno)
    return records, lines, rejects, bad_lines, total


def _failed(run_dir: Optional[Path], task: Path, unique_id: str, reason: str, cfg: dict,
            started: str) -> dict:
    # [v4.2] 血缘输出已停用：失败不再构建/追加血缘节点，代码保留存档备查。
    # node = lineage_util.build_lineage(
    #     OPERATOR, VERSION, unique_id, STAGE_ID, "", None, "failed", started,
    #     lineage_util.now_iso(), cfg, {"error": reason}, run_id=None, model=None)
    # lineage_util.append_lineage(task, node)
    # if run_dir is not None:
    #     lineage_util.snapshot(run_dir, "lineage.json", [node])
    return {"status": "failed", "reason": reason, "statistics": {}}


def run(input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None,
        run_id: Optional[str] = None) -> dict:
    """执行候选配对；产物只写入 OUTPUT_DIR/<uniqueId>/<operator>/<runId>/。"""
    started = lineage_util.now_iso()
    out_root = Path(output_root) if output_root else BASE_DIR / "output"
    task = op_io.task_dir(out_root, unique_id)

    try:
        cfg = validate_config(apply_overrides(load_config(config_path), overrides))
    except ConfigError as exc:
        return _failed(None, task, unique_id, "config_error: %s" % exc,
                       {"pairing": {"requested_overrides": overrides or {}}}, started)

    input_path = Path(input_file)
    if not input_path.is_file():
        return _failed(None, task, unique_id, "input_not_found", cfg, started)

    run_parent = task / OPERATOR
    run_parent.mkdir(parents=True, exist_ok=True)
    run_id = run_id or op_io.next_run_id(run_parent)
    run_dir = run_parent / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    logger = Logger(run_dir / "run.log")

    pairs_path = run_dir / MAIN_NAME
    unpaired_path = run_dir / UNPAIRED_NAME
    rejects_path = run_dir / REJECTS_NAME
    for target in (pairs_path, unpaired_path, rejects_path):
        if target.resolve() == input_path.resolve():
            shutil.rmtree(str(run_dir), ignore_errors=True)
            return _failed(None, task, unique_id, "output_conflicts_with_input", cfg, started)

    logger("输入", "加载输入文件：%s" % input_path.name)
    seen: Dict[str, int] = {}
    records, lines, rejects, bad_lines, total = _collect_inputs(input_path, seen)
    logger("读取", "读取 %d 条记录（坏行 %d 条，有效 %d 条）" % (total, bad_lines, len(records)))

    result = plan_pairs(records, cfg, unique_id=unique_id, now=started)
    line_of = dict(zip([record.get("sample_id", "").strip() for record in records], lines))
    for excess in result.rejects:
        rejects.append(_ledger(line_of.get(str(excess.get("sample_id")), 0),
                               excess.get("sample_id"), excess["reason"],
                               extra={"peer_sample_id": excess.get("peer_sample_id"),
                                      "group_level": excess.get("group_level")}))

    media_root = input_path.parent
    referenced = op_io.collect_media_paths(
        [pair["left"]["record"] for pair in result.pairs] +
        [pair["right"]["record"] for pair in result.pairs] +
        [item["record"] for item in result.unpaired])
    carried, missing = op_io.copy_media(run_dir, media_root, referenced)
    if missing:
        logger("警告", "有 %d 个被引用媒体文件不可读取，未伪造文件" % len(missing))

    op_io.atomic_write_jsonl(pairs_path, result.pairs, source_path=input_path)
    op_io.atomic_write_jsonl(unpaired_path, result.unpaired, source_path=input_path)
    op_io.atomic_write_jsonl(rejects_path, rejects, source_path=input_path)
    logger("处理", "候选配对 %d 对（A=%d B=%d C=%d），已配对记录 %d 条，未配对 %d 条" % (
        result.stats["pairs_total"], result.stats["pairs_by_evidence"]["A"],
        result.stats["pairs_by_evidence"]["B"], result.stats["pairs_by_evidence"]["C"],
        result.stats["paired_records"], result.stats["unpaired_records"]))

    artifacts = [{"path": MAIN_NAME, "role": "main"},
                 {"path": UNPAIRED_NAME, "role": "sidecar"},
                 {"path": REJECTS_NAME, "role": "review"}]
    for relative in carried:
        artifacts.append({"path": relative, "role": "media"})
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": MAIN_NAME,
                "artifacts": artifacts}
    lineage_util.snapshot(run_dir, "manifest.json", manifest)
    publish_stage_output(run_dir)
    logger("输出", "写入 %s / %s / %s 并携带媒体 %d 个" % (
        MAIN_NAME, UNPAIRED_NAME, REJECTS_NAME, len(carried)))

    input_level_rejects = len(rejects) - len(result.rejects) - bad_lines
    conservation = (total == len(records) + input_level_rejects + bad_lines
                    and len(rejects) == input_level_rejects + bad_lines + len(result.rejects)
                    and len(records) == result.stats["paired_records"] + result.stats["unpaired_records"]
                    and result.stats["pairs_total"] == sum(result.stats["pairs_by_evidence"].values()))
    if not conservation:
        logger("警告", "统计守恒校验未通过，请核对输入与清单")

    statistics = {
        # ---- 本算子明细口径 ----
        "input_records": total,
        "valid_records": result.stats["valid_records"],
        "derived_views": result.stats["derived_views"],
        "in_record_pairs": result.stats["in_record_pairs"],
        "paired_records": result.stats["paired_records"],
        "unpaired_records": result.stats["unpaired_records"],
        "pairs_total": result.stats["pairs_total"],
        "pairs_by_evidence": result.stats["pairs_by_evidence"],
        "groups_total": result.stats["groups_total"],
        "same_modality_pairs_skipped": result.stats["same_modality_pairs_skipped"],
        "group_pair_limit_hits": result.stats["group_pair_limit_hits"],
        "rejected_records": input_level_rejects,
        "bad_lines": bad_lines,
        "rejects_total": len(rejects),
        "media_carried": len(carried),
        "media_missing": len(missing),
        "empty_input": total == 0,
        "conservation_ok": conservation,
        # ---- 与兄弟算子共用的记录级口径（demo 05 §5）：input == output + rejected ----
        "input": total,
        "output": len(records),
        "rejected": input_level_rejects + bad_lines,
        "applicable": result.stats["paired_records"],
        "not_applicable": result.stats["unpaired_records"],
        # 本算子为纯规则实现，无模型后端，因此不存在降级态
        "degraded": False,
        "degradation_reason": None,
        "active_backend": "rule",
    }

    finished = lineage_util.now_iso()
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘节点、不追加任务血缘历史、
    # 不再落盘 lineage.json / stage_context.json；代码保留存档备查。
    # node = lineage_util.build_lineage(
    #     OPERATOR, VERSION, unique_id, STAGE_ID, str(input_path), str(pairs_path), "success",
    #     started, finished, cfg, statistics, run_id=run_id, model=None,
    #     artifacts=artifacts)
    # lineage_util.append_lineage(task, node)
    # stage_nodes, prior_nodes = lineage_util.partition_lineage(
    #     lineage_util.load_lineage_history(task), STAGE_ID, unique_id)
    # lineage_util.snapshot(run_dir, "lineage.json", stage_nodes)
    # lineage_util.snapshot(run_dir, "stage_context.json", {
    #     "stageId": STAGE_ID,
    #     "stageName": STAGE_NAME,
    #     "uniqueId": unique_id,
    #     "entrypoint": MAIN_NAME,
    #     "stage_lineage": stage_nodes,
    #     "prior_lineage": prior_nodes,
    # })
    logger("完成", "算子执行完成，状态 success，runId %s" % run_id)

    return {
        "status": "success",
        "runId": run_id,
        "runDir": str(run_dir),
        "outputFile": str(pairs_path),
        "unpairedFile": str(unpaired_path),
        "rejectsFile": str(rejects_path),
        "manifest": manifest,
        "statistics": statistics,
        # [v4.2] 血缘停用：返回值不再携带 "lineage" / "stageLineage" 键。
        "config": cfg,
        "stageId": STAGE_ID,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="算子 #11 生成候选配对")
    parser.add_argument("--input", required=True, help="准入通过对象的 JSONL 清单")
    parser.add_argument("--unique-id", required=True, help="业务键：任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 configs/config.yaml")
    parser.add_argument("--output-root", default=None, help="输出根目录，默认 output")
    args = parser.parse_args(argv)

    try:
        result = run(args.input, args.unique_id, args.config, args.output_root)
    except ValueError as exc:  # 非法 uniqueId 等入参问题：不打印原始堆栈
        print("result: failed reason=%s" % exc)
        return 1
    if result["status"] == "failed":
        print("result: failed reason=%s" % result.get("reason"))
        return 1
    stats = result["statistics"]
    print("result: success pairs=%d paired=%d unpaired=%d evidence=%s rejects=%d" % (
        stats["pairs_total"], stats["paired_records"], stats["unpaired_records"],
        json.dumps(stats["pairs_by_evidence"], ensure_ascii=False), stats["rejects_total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
