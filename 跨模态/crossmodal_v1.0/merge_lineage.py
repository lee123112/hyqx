# -*- coding: utf-8 -*-
# [v4.2] 血缘输出已停用：本文件不再参与运行，保留存档。
"""按原始输入源文件汇总全链路血缘。

业务交付口径：**一个原始输入文件最终对应一份血缘文件**（`!!血缘文件开发说明.md` 第 1 节
「一个输入文件一份 lineage_<uniqueId>.jsonl，跨阶段累计追加」）。链路运行期血缘按算子
run 目录分散落盘（`<算子>/output/<任务uid>/…/runNNN/lineage/`，含输入 ZIP 携带的上游
快照 `input/lineage/`），本工具把同一源文件在所有算子下的血缘行拼接成一份完整 jsonl：

```
file → ( stage_segment → oss_supplement ) × N → outcome → ( deliver_supplement )
```

合并规则（与说明第 2 节一致，身份字段绝不被覆盖）：
- `file`：取首次出现（各阶段身份 setdefault 继承，内容应一致）；
- `stage_segment`：拆到**算子节点**级按 (stage, operatorNumber) 归并——上游产物携带的
  继承快照、同任务重跑（run001/run002）、input 副本视为同一节点，正本
  （来源任务编号==算子编号）优先、时间最新者胜；每阶段主链路节点重组为一段（段内按
  算子编号升序）；专项运行（t1_21_reject / t1_23_check 等）的节点按独立事件保留为
  独立段；
- `oss_supplement`：主链路每 stage 一份（最新），专项按 (stage, 任务) 保留；
- `outcome` / `deliver_supplement`：主链路优先，取时间最新一条作为最终结论。

行序按 (阶段号 S1..S8, 主链路段在前) 稳定排序，输出前做行序自检。

用法：
    python merge_lineage.py --report <算子测试报告根> --out <输出目录> --uid <任务uid前缀>
    python merge_lineage.py --report 算子测试报告 --out 血缘汇总_按源文件 --uid t1

产物：
- `<out>/lineage_<uid>_<源文件标识>.jsonl`：每原始输入文件一份（份数与首个算子
  登记的源文件数一致，即原始输入记录数）；
- `<out>/merge_manifest.json`：每份血缘的行数、阶段覆盖、算子覆盖、最终 verdict；
- `<out>/README.md`：口径说明。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LINEAGE_NAME = re.compile(r"^lineage_(?P<uid>[^_]+)_(?P<source>.+?)\.jsonl$")
RUN_DIR = re.compile(r"run\d+$")


def load_map(package_root: Path) -> dict:
    """算子目录名 -> (正式编号, 中文名)。"""
    registry = json.loads((package_root / "map.json").read_text(encoding="utf-8"))
    out = {}
    for entry in (registry.get("operators") or {}).values():
        out[str(entry.get("package"))] = (int(entry.get("formalOperatorNumber", 0)),
                                          str(entry.get("name", "")))
    return out


def read_rows(path: Path) -> list:
    rows = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass  # 容忍个别坏行，如实少拼不如报错阻断——记录在 manifest.badLines
    return rows


def row_ts(row: dict) -> str:
    """排序用时间戳：outcome 有 startedAt/结束时无统一字段，取段内 ts 或文件 startedAt。"""
    for key in ("ts", "startedAt", "finishedAt", "endedAt"):
        if isinstance(row.get(key), str):
            return row[key]
    ops = row.get("operators") or []
    for op in ops:
        if isinstance(op, dict) and isinstance(op.get("ts"), str):
            return op["ts"]
    return ""


def stage_num(row: dict) -> int:
    stage = row.get("stage")
    if isinstance(stage, str) and stage.startswith("S"):
        try:
            return int(stage[1:])
        except ValueError:
            return 99
    return 99


def seg_key(row: dict) -> tuple:
    """stage_segment 去重键：(阶段, 任务uniqueId, 段内算子名集合)。

    runId 故意不进键：同任务重跑（run001/run002）视为同一段、保留最新；
    不同任务 uid（t1_23 / t1_23_check 等专项运行）是独立事件，天然不去重。
    """
def task_uid_num(task_uid: str, prefix: str) -> int:
    """t1_05 -> 5；t1_21_reject -> -1（专项任务非纯编号）。"""
    tail = task_uid[len(prefix) + 1:] if task_uid.startswith(prefix + "_") else ""
    return int(tail) if tail.isdigit() else -1


def is_main_task(task_uid: str, prefix: str) -> bool:
    """主链路任务：t1_05；专项任务：t1_21_reject / t1_23_check / t1_23_pending。"""
    return task_uid_num(task_uid, prefix) > 0


def node_ts(node: dict) -> str:
    return str(node.get("ts") or "")


def merge_source(entries: list, prefix: str) -> tuple[list, dict]:
    """entries: [(order, task_uid, is_input, rows)]。节点级归并后重组每阶段一段。

    归并规则：
    - 算子节点主键 = (stage, operatorNumber)；
      专项任务（task_uid 带后缀）的节点键额外含 task_uid，独立保留；
    - 同键多份（上游产物携带的继承快照 / 重跑 run001,run002 / input 副本）择优：
      正本（来源任务编号 == 节点算子编号） > run 目录 > input 快照；同级取 ts 最新；
    - 重组：同一 (stage, task_uid) 的节点合成一段，段内按 operatorNumber 升序；
    - oss_supplement 主链路每 stage 一份（最新），专项按 (stage, task_uid) 保留；
    - outcome / deliver_supplement：主链路优先，取 ts 最新（专项运行不覆盖主链路结论）。
    """
    file_row = None
    nodes: dict[tuple, dict] = {}
    oss: dict[tuple, dict] = {}
    outcomes: list[tuple[str, dict]] = []   # (task_uid, row)
    delivers: list[tuple[str, dict]] = []
    stats = {"sources": len(entries), "identityConflicts": 0, "mergedNodes": 0,
             "specialTaskSegs": 0}

    for order, task_uid, is_input, rows in entries:
        task_num = task_uid_num(task_uid, prefix)
        main_task = is_main_task(task_uid, prefix)
        for row in rows:
            kind = row.get("type")
            if kind == "file":
                if file_row is None:
                    file_row = row
                else:
                    for key in ("fileId", "file", "checksum", "bytes"):
                        if file_row.get(key) != row.get(key):
                            stats["identityConflicts"] += 1
            elif kind == "stage_segment":
                stage = str(row.get("stage") or "")
                for node in row.get("operators") or []:
                    if not isinstance(node, dict):
                        continue
                    try:
                        op_num = int(node.get("operatorNumber") or 0)
                    except (TypeError, ValueError):
                        op_num = 0
                    key = (stage, op_num) if main_task else (stage, op_num, task_uid)
                    score = (1 if (not is_input and task_num == op_num) else 0,
                             0 if is_input else 1,
                             node_ts(node))
                    best = nodes.get(key)
                    if best is None or score > best["score"]:
                        nodes[key] = {"node": node, "score": score,
                                      "task_uid": "" if main_task else task_uid,
                                      "stage": stage, "op_num": op_num}
            elif kind == "oss_supplement":
                stage = str(row.get("stage") or "")
                key = (stage,) if main_task else (stage, task_uid)
                if key not in oss or row_ts(row) >= row_ts(oss[key]):
                    oss[key] = row
            elif kind == "outcome":
                outcomes.append((("" if main_task else task_uid), row))
            elif kind == "deliver_supplement":
                delivers.append((("" if main_task else task_uid), row))

    # 重组：按 (stage, task_uid) 分组成段，段内节点按算子编号升序
    groups: dict[tuple, list] = {}
    for entry in nodes.values():
        groups.setdefault((entry["stage"], entry["task_uid"]), []).append(entry)
    merged: list = []
    if file_row is not None:
        merged.append(file_row)
    def stage_sort_key(group_key: tuple) -> tuple:
        stage, task_uid = group_key
        return (stage_num({"stage": stage}), 0 if not task_uid else 1, task_uid)
    for group_key in sorted(groups, key=stage_sort_key):
        members = sorted(groups[group_key], key=lambda e: e["op_num"])
        stage, task_uid = group_key
        seg = {"type": "stage_segment", "writtenBy": "operator", "stage": stage,
               "name": (members[0]["node"].get("stageName")
                        or members[0]["node"].get("stage_label") or stage),
               "operators": [m["node"] for m in members]}
        merged.append(seg)
        if task_uid:
            stats["specialTaskSegs"] += 1
        oss_key = (stage, task_uid) if task_uid else (stage,)
        if oss_key in oss:
            merged.append(oss[oss_key])

    def latest(rows: list) -> dict | None:
        return max(rows, key=lambda item: row_ts(item[1]))[1] if rows else None

    main_outcomes = [item for item in outcomes if not item[0]]
    final_outcome = latest(main_outcomes) or latest(outcomes)
    if final_outcome is not None:
        merged.append(final_outcome)
    main_delivers = [item for item in delivers if not item[0]]
    final_deliver = latest(main_delivers) or latest(delivers)
    if final_deliver is not None:
        merged.append(final_deliver)

    stats["stages"] = sorted({k[0] for k in groups})
    stats["operators"] = sorted({str(e["node"].get("operator")) for e in nodes.values()})
    stats["operatorNumbers"] = sorted({e["op_num"] for e in nodes.values()})
    stats["finalVerdict"] = (final_outcome or {}).get("verdict")
    stats["lines"] = len(merged)
    stats["mergedNodes"] = len(nodes)
    return merged, stats


def validate_order(rows: list) -> list[str]:
    errs = []
    kinds = [r.get("type") for r in rows]
    if not kinds or kinds[0] != "file":
        errs.append("首行不是 file")
    if kinds.count("file") != 1:
        errs.append("file 行数 %d" % kinds.count("file"))
    if kinds.count("outcome") > 1:
        errs.append("outcome 行数 %d" % kinds.count("outcome"))
    if "outcome" in kinds and kinds[-1] not in ("outcome", "deliver_supplement"):
        errs.append("末行不是 outcome/deliver_supplement")
    body = kinds[1:kinds.index("outcome")] if "outcome" in kinds else kinds[1:]
    for kind in body:
        if kind not in ("stage_segment", "oss_supplement"):
            errs.append("段间非法行 %s" % kind)
            break
    nums = [stage_num(r) for r in rows if r.get("type") == "stage_segment"]
    for a, b in zip(nums, nums[1:]):
        if b < a:
            errs.append("阶段号倒序 %s" % nums)
            break
    return errs


def main() -> int:
    parser = argparse.ArgumentParser(description="按原始输入源文件汇总全链路血缘")
    parser.add_argument("--report", required=True, help="算子测试报告根目录")
    parser.add_argument("--out", required=True, help="输出目录（每源文件一份 jsonl）")
    parser.add_argument("--uid", required=True, help="任务 uid 前缀（如 t1）")
    parser.add_argument("--package", default=str(Path(__file__).resolve().parent),
                        help="交付包根目录（读 map.json；默认脚本所在目录）")
    args = parser.parse_args()

    report = Path(args.report)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    op_map = load_map(Path(args.package))

    # 收集所有 runNNN/lineage 与 input/lineage 的血缘文件，按 (算子编号, 任务uid, run) 定序
    by_source: dict[str, list] = {}
    for lineage_dir in sorted(report.rglob("lineage")):
        if not lineage_dir.is_dir():
            continue
        run_dir = lineage_dir.parent
        is_run = bool(RUN_DIR.search(run_dir.name))
        is_input = "input" in run_dir.parts
        if not (is_run or is_input):
            continue
        # 算子目录名：路径中找 CMA_*；任务 uid：路径中找 <uid> 段或 <uid>_* 段
        op_name = next((p for p in lineage_dir.parts if p.startswith("CMA_")), "")
        op_num, _op_label = op_map.get(op_name, (99, ""))
        task_uid = ""
        for part in lineage_dir.parts:
            if part == args.uid or part.startswith(args.uid + "_"):
                task_uid = part
                break
        run_no = run_dir.name if is_run else "input"
        order = (op_num, task_uid, 0 if is_run else 1, run_no)
        for path in sorted(lineage_dir.glob("lineage_*.jsonl")):
            match = LINEAGE_NAME.match(path.name)
            if not match or match.group("uid") != args.uid:
                continue
            source = match.group("source")
            by_source.setdefault(source, []).append(
                (order, task_uid, is_input, path))

    if not by_source:
        print("[merge] 未发现血缘文件（--report/--uid 是否正确？）")
        return 1

    manifest, bad = [], 0
    for source in sorted(by_source):
        entries = [(order, task_uid, is_input, read_rows(path))
                   for order, task_uid, is_input, path in sorted(by_source[source])]
        merged, stats = merge_source(entries, args.uid)
        errs = validate_order(merged)
        target = out_dir / ("lineage_%s_%s.jsonl" % (args.uid, source))
        target.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged),
                          encoding="utf-8")
        manifest.append({"source": source, "file": target.name, "errors": errs, **stats})
        if errs:
            bad += 1

    total_lines = sum(m["lines"] for m in manifest)
    summary = {
        "taskUid": args.uid,
        "sourceFiles": len(manifest),
        "totalLines": total_lines,
        "filesWithOrderErrors": bad,
        "note": "每个原始输入文件一份全链路血缘；算子节点按(阶段,算子编号)归并——继承快照/"
                "重跑/input 副本择一（正本优先、时间最新），每阶段主链路重组为一段；专项运行"
                "（如 t1_21_reject）作为独立事件段保留；outcome 以主链路最新为准。",
    }
    (out_dir / "merge_manifest.json").write_text(
        json.dumps({"summary": summary, "sources": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    readme = [
        "# 血缘汇总（按原始输入文件）", "",
        "本目录每个 `lineage_{uid}_<源文件标识>.jsonl` 对应一个原始输入文件，内容由该源文件"
        "在全部算子运行目录下的血缘行拼接而成：`file → (stage_segment → oss_supplement)×N → "
        "outcome → (deliver_supplement)`。", "",
        "- 源文件份数：**%d**（与首个算子登记的原始输入记录数一致）；" % len(manifest),
        "- 血缘总行数：%d；行序违规文件：%d；" % (total_lines, bad),
        "- 归并口径：算子节点按（阶段, 算子编号）去重——上游产物携带的继承快照、重跑"
        "（run001/run002）、input 副本择一保留（正本优先、时间最新）；每阶段主链路重组为"
        "一段；专项运行（如 t1_21_reject / t1_23_check）作为独立事件段保留；`outcome` 以"
        "主链路最新为准；",
        "- 明细见 `merge_manifest.json`（每源文件的阶段覆盖、算子覆盖、最终 verdict）；",
        "- `identityConflicts`：该源文件各份血缘 `file` 行身份字段（file/checksum/bytes）"
        "不一致的次数——说明第 2.1 节身份继承被下游算子改写（如 #07 标准化去扩展名后"
        "重写 file 行），汇总口径取**首次出现**（原始身份），冲突如实计数供整改。",
    ]
    (out_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    print("[merge] 源文件 %d 份 | 总行数 %d | 行序违规 %d -> %s"
          % (len(manifest), total_lines, bad, out_dir))
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
