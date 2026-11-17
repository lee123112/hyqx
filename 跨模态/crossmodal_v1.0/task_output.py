# -*- coding: utf-8 -*-
# [v4.2] 血缘输出已停用：本文件不再参与运行，保留存档。
"""任务级最终输出：阶段末算子把“本大阶段完整血缘”增量写入 ``lineage/jsonl``。

业务口径（见 ``跨模态正式开发方案/输出格式.md``）：

- 一个原始输入文件只对应一份合并血缘 ``lineage_<任务id>_<文件id>.jsonl``；
- S1–S8 的阶段末算子（M03/M08/M19/M21/M22/M23/M25/M26）负责把**本阶段全部算子**
  的节点合成一个 ``stage_segment``，写入最终文件；
- 非阶段末算子不写任务级输出；
- S6 M23 额外把交付包放到 ``data/zip/``。

阶段末算子运行时，本模块扫描同一任务、同一阶段各算子的 run 目录，按
``operatorNumber`` 合并节点；这样即使阶段内算子没有把上游血缘透传下来，阶段末
仍能得到完整的本阶段血缘。找不到其它 run 时退回当前 run 的逐文件血缘。

最终目录：``TASK_OUTPUT_ROOT/<任务id>/output/``。任何异常都只记日志，不影响
``/predict`` 响应。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "STAGE_LAST_NUMBERS",
    "STAGE_OPERATORS",
    "merge_stage_rows",
    "publish_stage",
    "replay_stage_last_runs",
    "task_output_dir",
    "validate_rows",
]

#: S1–S8 的阶段末算子（规范 §3.4；M24 不是阶段末）
STAGE_LAST_NUMBERS: Tuple[int, ...] = (3, 8, 19, 21, 22, 23, 25, 26)

#: 阶段 → 阶段内算子编号（与 mty_compliance.CHAIN_STAGE_PLAN 一致）
STAGE_OPERATORS: Dict[str, Tuple[int, ...]] = {
    "S1": (1, 2, 3),
    "S2": (4, 5, 6, 7, 8),
    "S3": (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19),
    "S4": (20, 21),
    "S5": (22,),
    "S6": (23,),
    "S7": (24, 25),
    "S8": (26,),
}

LINEAGE_SUBDIR: Tuple[str, ...] = ("lineage", "jsonl")
DATA_SUBDIR: Tuple[str, ...] = ("data", "zip")
LOCK_NAME = ".task_output.lock"
LOCK_TIMEOUT_SECONDS = 600
DELIVERY_OPERATOR_NUMBER = 23

_STAGE_RE = re.compile(r"^S?(\d+)$")
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")
_LINEAGE_NAME_RE = re.compile(r"^lineage_.+\.jsonl$")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _stage_rank(stage: Any) -> int:
    match = _STAGE_RE.match(str(stage or "").strip())
    return int(match.group(1)) if match else 99


def _stage_id(rank: int) -> str:
    return "S%d" % rank


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _rows_text(rows: Sequence[Dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".%s.tmp-%d-%d" % (path.name, os.getpid(), time.time_ns()))
    try:
        with open(str(temp), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(".%s.tmp-%d-%d" % (target.name, os.getpid(), time.time_ns()))
    try:
        shutil.copy2(str(source), str(temp))
        os.replace(str(temp), str(target))
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #

def _segments(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if row.get("type") == "stage_segment"]


def _last_of(rows: Iterable[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    found = None
    for row in rows:
        if row.get("type") == kind:
            found = row
    return found


def _renumber(rows: Sequence[Dict[str, Any]]) -> None:
    seq = 0
    for row in rows:
        if row.get("type") != "stage_segment":
            continue
        for node in row.get("operators") or []:
            if not isinstance(node, dict):
                continue
            seq += 1
            node["seq"] = seq


#: 后续阶段“不适用/未抽中”类 PENDING，不覆盖已交付的 PASS
_NONBLOCKING_PENDING_MARKERS = (
    "quota", "not_sampled", "not sampled", "not_applicable", "not applicable",
    "skipped", "disabled", "no_sample", "not_sample",
)
_TERMINAL_VERDICTS = ("REJECT", "FAIL")


def _is_nonblocking_pending(row: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(row, dict) or row.get("verdict") != "PENDING":
        return False
    text = " ".join(str(row.get(key) or "") for key in ("reason", "reason_code", "code")).lower()
    return any(marker in text for marker in _NONBLOCKING_PENDING_MARKERS)


def _outcome_severity(verdict: Any) -> int:
    """阶段内多条结论取严重级：REJECT/FAIL > PENDING > PASS。"""
    verdict = str(verdict or "").upper()
    if verdict in _TERMINAL_VERDICTS:
        return 2
    if verdict == "PENDING":
        return 1
    return 0


def merge_stage_rows(prior_rows: Sequence[Dict[str, Any]],
                     current_rows: Sequence[Dict[str, Any]],
                     stage_id: str) -> Optional[List[Dict[str, Any]]]:
    """把本次阶段血缘合并进已有最终血缘；无当前阶段记录时返回 ``None``。

    规则：
    - 已有阶段原样保留；当前阶段段/oss 替换已有同阶段内容；
    - 已有文件缺某个较早阶段时，用本次携带的同阶段内容补齐；
    - ``file`` 行优先取已有（身份字段不被下游改写）；
    - ``outcome`` 取合并后最深阶段对应的结论；
    - ``operators[].seq`` 从 1 连续重排。
    """
    stage_id = _stage_id(_stage_rank(stage_id))
    current_rank = _stage_rank(stage_id)
    current_segments = [row for row in _segments(current_rows)
                        if _stage_id(_stage_rank(row.get("stage"))) == stage_id]
    if not current_segments:
        return None

    prior_file = _last_of(prior_rows, "file")
    current_file = _last_of(current_rows, "file")
    file_row = prior_file or current_file
    if not isinstance(file_row, dict):
        return None

    segments: Dict[int, Dict[str, Any]] = {}
    for row in _segments(prior_rows):
        segments[_stage_rank(row.get("stage"))] = row
    for row in _segments(current_rows):
        rank = _stage_rank(row.get("stage"))
        if rank == current_rank or rank not in segments:
            segments[rank] = row

    oss_rows: Dict[int, Dict[str, Any]] = {}
    for row in prior_rows:
        if row.get("type") == "oss_supplement":
            oss_rows[_stage_rank(row.get("stage"))] = row
    for row in current_rows:
        if row.get("type") != "oss_supplement":
            continue
        rank = _stage_rank(row.get("stage"))
        if rank == current_rank or rank not in oss_rows:
            oss_rows[rank] = row

    prior_outcome = _last_of(prior_rows, "outcome")
    current_outcome = _last_of(current_rows, "outcome")
    prior_deepest = max((_stage_rank(row.get("stage")) for row in _segments(prior_rows)),
                        default=-1)
    merged_deepest = max(segments.keys(), default=-1)
    prior_verdict = str((prior_outcome or {}).get("verdict") or "").upper()
    outcome = current_outcome if current_outcome is not None else prior_outcome
    if prior_verdict in _TERMINAL_VERDICTS:
        # 已进入隔离/失败台账的文件结论封存，后续阶段不得改写。
        outcome = prior_outcome
    elif (current_outcome is not None and prior_outcome is not None
            and prior_outcome.get("verdict") == "PASS"
            and _is_nonblocking_pending(current_outcome)):
        # S7/S8 的“未抽中/不适用”PENDING 不覆盖 S6 已交付 PASS。
        outcome = prior_outcome
    elif current_outcome is not None and current_rank >= prior_deepest:
        outcome = current_outcome
    elif prior_outcome is not None:
        outcome = prior_outcome
    # 最深阶段不是当前阶段时，绝不拿当前阶段结论覆盖更深阶段结论。
    if merged_deepest > current_rank and prior_outcome is not None:
        outcome = prior_outcome

    prior_deliver = _last_of(prior_rows, "deliver_supplement")
    current_deliver = _last_of(current_rows, "deliver_supplement")
    deliver = current_deliver or prior_deliver

    merged: List[Dict[str, Any]] = [file_row]
    for rank in sorted(segments):
        merged.append(segments[rank])
        if rank in oss_rows:
            merged.append(oss_rows[rank])
    if outcome is not None:
        merged.append(outcome)
    if deliver is not None:
        merged.append(deliver)

    _renumber(merged)
    return merged


def validate_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """返回校验错误列表；空列表表示合规。"""
    errors: List[str] = []
    kinds = [row.get("type") for row in rows]
    if not kinds or kinds[0] != "file":
        errors.append("首行不是 file")
    if kinds.count("file") != 1:
        errors.append("file 行数 %d" % kinds.count("file"))
    if kinds.count("outcome") != 1:
        errors.append("outcome 行数 %d" % kinds.count("outcome"))
    if kinds and kinds[-1] not in ("outcome", "deliver_supplement"):
        errors.append("末行不是 outcome/deliver_supplement")

    ranks: List[int] = []
    seq: List[Any] = []
    for row in rows:
        if row.get("type") != "stage_segment":
            continue
        ranks.append(_stage_rank(row.get("stage")))
        for node in row.get("operators") or []:
            if not isinstance(node, dict):
                errors.append("operators 含非对象")
                continue
            if node.get("operatorNumber") in (None, ""):
                errors.append("operatorNumber 为空")
            seq.append(node.get("seq"))
    if any(b < a for a, b in zip(ranks, ranks[1:])):
        errors.append("阶段号倒序 %s" % ranks)
    if seq != list(range(1, len(seq) + 1)):
        errors.append("seq 不连续 %s" % seq[:5])

    for row in rows:
        if row.get("type") == "outcome":
            if bool(row.get("delivered")) != (row.get("verdict") == "PASS"):
                errors.append("delivered 与 verdict 不一致")

    text = _rows_text(rows)
    if _ABS_PATH_RE.search(text):
        errors.append("出现宿主机绝对路径")
    return errors


# --------------------------------------------------------------------------- #
# 输出目录、锁与主链路判断
# --------------------------------------------------------------------------- #

def task_output_dir(output_root: Path, task_id: str) -> Path:
    return Path(output_root) / task_id / "output"


def _find_task_dir(run_dir: Path, task_id: str) -> Optional[Path]:
    """``run`` 目录向上找到 ``<任务id>_<阶段编号>`` 任务目录。"""
    for parent in Path(run_dir).resolve().parents:
        name = parent.name
        if name == task_id or name.startswith(task_id + "_"):
            return parent
    return None


def _run_number(task_dir_name: str, task_id: str) -> Optional[int]:
    if not task_dir_name.startswith(task_id + "_"):
        return None
    tail = task_dir_name[len(task_id) + 1:]
    return int(tail) if tail.isdigit() else None


def _output_root(explicit: Optional[Path], task_dir: Path) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("TASK_OUTPUT_ROOT") or os.environ.get("OUTPUT_DIR")
    if env:
        return Path(env)
    return task_dir.parent


def _acquire_lock(directory: Path, timeout: int = LOCK_TIMEOUT_SECONDS) -> Optional[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / LOCK_NAME
    deadline = time.time() + max(1, timeout)
    while True:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("ascii", "ignore"))
            os.close(handle)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0
            if age > timeout:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def _release_lock(lock: Optional[Path]) -> None:
    if lock is None:
        return
    try:
        lock.unlink()
    except OSError:
        pass


def _package_path(result: Dict[str, Any], run_dir: Path) -> Optional[Path]:
    raw = result.get("packagePath") or result.get("package_path")
    if isinstance(raw, str) and raw:
        path = Path(raw)
        if path.is_file():
            return path
        candidate = run_dir / Path(raw).name
        if candidate.is_file():
            return candidate
    return None


def _copy_package(package: Path, output_dir: Path) -> str:
    zip_dir = output_dir.joinpath(*DATA_SUBDIR)
    zip_dir.mkdir(parents=True, exist_ok=True)
    target = zip_dir / package.name
    _atomic_copy_file(package, target)
    for old in zip_dir.glob("*.zip"):
        if old.name != target.name:
            try:
                old.unlink()
            except OSError:
                pass
    return target.name


# --------------------------------------------------------------------------- #
# 阶段内全部 run 的发现与节点合并
# --------------------------------------------------------------------------- #

def _iter_run_dirs(task_dir: Path) -> List[Path]:
    runs = [path for path in task_dir.glob("*/run*") if path.is_dir()]
    return sorted(runs, key=lambda path: (int(re.sub(r"\D", "", path.name) or 0), str(path)))


def _run_sort_key(run_dir: Path) -> int:
    return int(re.sub(r"\D", "", Path(run_dir).name) or 0)


def _latest_run(task_dir: Path) -> Optional[Path]:
    for run_dir in reversed(_iter_run_dirs(task_dir)):
        lineage_dir = run_dir / "lineage"
        if lineage_dir.is_dir() and any(lineage_dir.glob("lineage_*.jsonl")):
            return run_dir
    return None


def discover_stage_runs(scan_root: Path, task_id: str, stage_id: str
                        ) -> Dict[int, Path]:
    """在共享输出根下找 ``<任务id>_<编号>/**/runNNN``；每个编号取最新 run。

    兼容 ``t2_3``/``t2_03`` 两种编号写法：按 ``任务id_`` 前缀扫描后解析尾号。
    """
    wanted = set(STAGE_OPERATORS.get(_stage_id(_stage_rank(stage_id)), ()))
    runs: Dict[int, Path] = {}
    for task_dir in _candidate_task_dirs(Path(scan_root), task_id):
        number = _run_number(task_dir.name, task_id)
        if number not in wanted:
            continue
        run_dir = _latest_run(task_dir)
        if run_dir is None:
            continue
        previous = runs.get(number)
        if previous is None or _run_sort_key(run_dir) > _run_sort_key(previous):
            runs[number] = run_dir
    return runs


def _candidate_task_dirs(root: Path, task_id: str) -> List[Path]:
    """兼容两种布局：``root/<任务id>_xx`` 与 ``root/<算子>/output/<任务id>_xx``。"""
    root = Path(root)
    found: List[Path] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(task_id + "_"):
            found.append(child)
            continue
        output = child / "output"
        if output.is_dir():
            found.extend(path for path in sorted(output.iterdir())
                         if path.is_dir() and path.name.startswith(task_id + "_"))
    return found


def discover_stage_runs_from_report(report_root: Path, task_id: str, stage_id: str
                                    ) -> Dict[int, Path]:
    """测试报告布局：各算子 ``output/<任务id>_<编号>`` 分散在不同目录下。"""
    wanted = set(STAGE_OPERATORS.get(_stage_id(_stage_rank(stage_id)), ()))
    runs: Dict[int, Path] = {}
    for task_dir in _candidate_task_dirs(Path(report_root), task_id):
        number = _run_number(task_dir.name, task_id)
        if number not in wanted:
            continue
        run_dir = _latest_run(task_dir)
        if run_dir is None:
            continue
        previous = runs.get(number)
        if previous is None or _run_sort_key(run_dir) > _run_sort_key(previous):
            runs[number] = run_dir
    return runs


def _has_stage_segment(rows: Sequence[Dict[str, Any]], stage_id: str) -> bool:
    rank = _stage_rank(stage_id)
    return any(_stage_rank(row.get("stage")) == rank for row in _segments(rows))


def _merge_stage_entries(entries: Sequence[Tuple[int, Sequence[Dict[str, Any]]]],
                         stage_id: str) -> Optional[List[Dict[str, Any]]]:
    """把同一阶段多个算子的血缘合并成一个完整 ``stage_segment``。"""
    rank = _stage_rank(stage_id)
    usable = [(number, rows) for number, rows in entries if _has_stage_segment(rows, stage_id)]
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])

    file_row: Optional[Dict[str, Any]] = None
    stage_name = _stage_id(rank)
    nodes: Dict[str, Dict[str, Any]] = {}
    oss: Optional[Dict[str, Any]] = None
    outcomes: List[Tuple[int, Dict[str, Any]]] = []
    delivers: List[Tuple[int, Dict[str, Any]]] = []

    for _number, rows in usable:
        if file_row is None:
            file_row = _last_of(rows, "file")
        for row in rows:
            kind = row.get("type")
            if kind == "stage_segment" and _stage_rank(row.get("stage")) == rank:
                stage_name = row.get("name") or stage_name
                for node in row.get("operators") or []:
                    if not isinstance(node, dict):
                        continue
                    key = str(node.get("operatorNumber") or "")
                    if not key:
                        key = "op:%s" % node.get("operator")
                    old = nodes.get(key)
                    if old is None or str(node.get("ts") or "") >= str(old.get("ts") or ""):
                        nodes[key] = node
            elif kind == "oss_supplement" and _stage_rank(row.get("stage")) == rank:
                oss = row
            elif kind == "deliver_supplement":
                delivers.append((_number, row))
        found = _last_of(rows, "outcome")
        if found is not None:
            outcomes.append((_number, found))

    if not isinstance(file_row, dict) or not nodes:
        return None
    ordered = sorted(nodes.values(),
                     key=lambda node: (int(node.get("operatorNumber") or 0),
                                       str(node.get("ts") or "")))
    segment = {"type": "stage_segment", "writtenBy": "operator",
               "stage": _stage_id(rank), "name": stage_name, "operators": ordered}
    merged: List[Dict[str, Any]] = [file_row, segment]
    if oss is not None:
        merged.append(oss)
    if outcomes:
        # 同一阶段内各算子独立运行时结论可能不一致：先按严重级，再取编号最深的一条。
        outcome = max(outcomes,
                      key=lambda item: (_outcome_severity(item[1].get("verdict")), item[0]))[1]
        merged.append(outcome)
    if delivers:
        # 交付补充行（M23 封版成功时产出）同样取编号最深的一条，交给
        # merge_stage_rows 与上一阶段遗留的交付行按口径合并。
        merged.append(max(delivers, key=lambda item: item[0])[1])
    return merged


def collect_stage_bundles(run_map: Dict[int, Path], stage_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """按源文件汇总阶段内各算子血缘；key 为 ``lineage_<任务id>_<文件id>.jsonl``。"""
    per_source: Dict[str, List[Tuple[int, Sequence[Dict[str, Any]]]]] = {}
    for number, run_dir in sorted(run_map.items()):
        lineage_dir = Path(run_dir) / "lineage"
        if not lineage_dir.is_dir():
            continue
        for path in sorted(lineage_dir.glob("lineage_*.jsonl")):
            rows = _read_rows(path)
            if rows:
                per_source.setdefault(path.name, []).append((number, rows))
    bundles: Dict[str, List[Dict[str, Any]]] = {}
    for name, entries in per_source.items():
        bundle = _merge_stage_entries(entries, stage_id)
        if bundle is not None:
            bundles[name] = bundle
    return bundles


# --------------------------------------------------------------------------- #
# 发布入口
# --------------------------------------------------------------------------- #

def publish_stage(*, run_dir: Path, lineage_files: Sequence[Path] = (),
                  result: Optional[Dict[str, Any]] = None, task_id: str,
                  stage_id: str, operator_number: int,
                  output_root: Optional[Path] = None,
                  scan_root: Optional[Path] = None,
                  run_map: Optional[Dict[int, Path]] = None,
                  enforce_main_run: bool = True) -> Dict[str, Any]:
    """阶段末发布：把本阶段完整血缘增量写入任务级输出。

    返回摘要字典；任何文件级失败都记录在 ``errors`` 中，不抛异常。
    """
    result = result or {}
    stage_id = _stage_id(_stage_rank(stage_id))
    summary: Dict[str, Any] = {
        "taskId": task_id, "stage": stage_id, "operatorNumber": operator_number,
        "written": 0, "skipped": 0, "errors": [], "outputDir": "",
        "package": "", "locked": False, "sources": 0,
    }
    if os.environ.get("TASK_OUTPUT_ENABLED", "1").strip() == "0":
        summary["skippedReason"] = "disabled"
        return summary

    run_dir = Path(run_dir)
    task_dir = _find_task_dir(run_dir, task_id)
    if task_dir is None:
        summary["skippedReason"] = "task-dir-not-found"
        return summary
    run_number = _run_number(task_dir.name, task_id)
    if enforce_main_run and run_number != int(operator_number):
        summary["skippedReason"] = "not-main-run"
        return summary

    output_dir = task_output_dir(_output_root(output_root, task_dir), task_id)
    summary["outputDir"] = str(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(output_dir)
    if lock is None:
        summary["locked"] = True
        summary["skippedReason"] = "locked"
        return summary

    try:
        discovery_root = scan_root
        if discovery_root is None:
            env = os.environ.get("TASK_OUTPUT_SCAN_ROOT")
            discovery_root = Path(env) if env else _output_root(output_root, task_dir)
        runs: Dict[int, Path] = dict(run_map or {})
        if not runs:
            runs = discover_stage_runs(Path(discovery_root), task_id, stage_id)
        runs.setdefault(int(operator_number), run_dir)
        bundles = collect_stage_bundles(runs, stage_id)
        if not bundles and lineage_files:
            bundles = collect_stage_bundles({int(operator_number): run_dir}, stage_id)
        summary["sources"] = len(bundles)

        lineage_dir = output_dir.joinpath(*LINEAGE_SUBDIR)
        for name in sorted(bundles):
            current_rows = bundles[name]
            target = lineage_dir / name
            prior_rows = _read_rows(target)
            merged = merge_stage_rows(prior_rows, current_rows, stage_id)
            if merged is None:
                summary["skipped"] += 1
                continue
            errors = validate_rows(merged)
            if errors:
                summary["errors"].append({"file": name, "errors": errors})
                continue
            _atomic_write_text(target, _rows_text(merged))
            summary["written"] += 1

        if int(operator_number) == DELIVERY_OPERATOR_NUMBER:
            package = _package_path(result, run_dir)
            if package is None:
                candidate = runs.get(DELIVERY_OPERATOR_NUMBER, run_dir)
                zips = sorted(Path(candidate).glob("23_*package.zip"))
                deliverable = [path for path in zips if "deliverable" in path.name]
                package = (deliverable or zips or [None])[0]
            if package is not None:
                summary["package"] = _copy_package(Path(package), output_dir)
    except Exception as exc:                              # noqa: BLE001 - 绝不阻断业务
        summary["errors"].append({"file": "", "errors": [type(exc).__name__ + ": " + str(exc)]})
    finally:
        _release_lock(lock)
    return summary


# --------------------------------------------------------------------------- #
# 验收回放：按 S1→S8 把测试报告里的阶段末 run 依次写入任务输出
# --------------------------------------------------------------------------- #

_STAGE_LAST_BY_RANK: Dict[int, int] = {}
for _stage, _numbers in STAGE_OPERATORS.items():
    _last = next((number for number in STAGE_LAST_NUMBERS if number in _numbers), None)
    if _last is not None:
        _STAGE_LAST_BY_RANK[_stage_rank(_stage)] = _last


def replay_stage_last_runs(report_root: Path, task_id: str, output_root: Path,
                           package_root: Optional[Path] = None) -> Dict[str, Any]:
    """把 ``report_root`` 中某任务 S1–S8 的阶段末 run 依次回放进任务输出。"""
    summary: Dict[str, Any] = {"taskId": task_id, "stages": [], "files": 0, "errors": []}
    out_dir = task_output_dir(Path(output_root), task_id)
    for rank in sorted(_STAGE_LAST_BY_RANK):
        stage_id = _stage_id(rank)
        stage_last = _STAGE_LAST_BY_RANK[rank]
        runs = discover_stage_runs_from_report(Path(report_root), task_id, stage_id)
        if stage_last not in runs:
            summary["errors"].append({"stage": stage_id, "errors": ["阶段末 run 缺失"]})
            continue
        run_dir = runs[stage_last]
        result: Dict[str, Any] = {}
        if stage_last == DELIVERY_OPERATOR_NUMBER:
            zips = sorted(Path(run_dir).glob("23_*package.zip"))
            deliverable = [path for path in zips if "deliverable" in path.name]
            package = (deliverable or zips or [None])[0]
            if package is not None:
                result["packagePath"] = str(package)
        item = publish_stage(
            run_dir=Path(run_dir), lineage_files=(), result=result,
            task_id=task_id, stage_id=stage_id, operator_number=stage_last,
            output_root=Path(output_root), run_map=runs, enforce_main_run=True)
        item["runDir"] = str(run_dir)
        summary["stages"].append(item)
        summary["files"] += item.get("written", 0)
        summary["errors"].extend(item.get("errors") or [])
    summary["outputDir"] = str(out_dir)
    return summary


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="任务级输出：阶段末血缘增量合并 / 回放")
    parser.add_argument("--replay-report", required=True,
                        help="测试报告根目录（内含各算子 output/<任务uid>/.../runNNN）")
    parser.add_argument("--task-id", required=True, help="任务 id（如 t1）")
    parser.add_argument("--out", required=True, help="TASK_OUTPUT_ROOT（输出根）")
    parser.add_argument("--package-root", default=str(Path(__file__).resolve().parent),
                        help="算子包根目录（保留参数，当前回放不依赖）")
    args = parser.parse_args(argv)

    summary = replay_stage_last_runs(Path(args.replay_report), args.task_id,
                                     Path(args.out), Path(args.package_root))
    for item in summary["stages"]:
        print("[stage] %s op=%s written=%s skipped=%s sources=%s package=%s" % (
            item.get("stage"), item.get("operatorNumber"), item.get("written"),
            item.get("skipped"), item.get("sources"), item.get("package") or "-"))
    print("[replay] task=%s output=%s written=%d errors=%d" % (
        summary["taskId"], summary["outputDir"], summary["files"], len(summary["errors"])))
    for error in summary["errors"][:20]:
        print("  [error]", error)
    return 0 if not summary["errors"] else 2


if __name__ == "__main__":
    sys.exit(_main())
