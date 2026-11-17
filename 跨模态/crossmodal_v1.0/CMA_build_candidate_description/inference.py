"""算子 #09 准备候选描述。

规则型算子：按表1固定优先级从图注、Alt、标题、字幕、语音转写、人工补写中
抽取 5–500 字的第一条有效描述；文件名和标签无效；无有效描述的样本只生成
人工补写任务并留在产线，不进入下游准入，也不进入任何隔离区。

本算子不加载、不调用任何图像描述模型（BLIP/LLaVA/OCR 一律不部署），
因此不存在模型分数或降级分数。
"""
from __future__ import annotations

import argparse
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

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils.description import (build_rewrite_task, extract_candidate,  # noqa: E402
                               SOURCE_LABELS, SOURCE_PRIORITY)
from utils.io import (AtomicJsonlWriter, read_jsonl, resolve_in_task,  # noqa: E402
                      task_dir, write_jsonl_atomic)
# [v4.2] 血缘输出已停用：build_lineage/append_lineage 不再调用（函数保留在 utils/lineage.py 存档）
# from utils.lineage import append_lineage, build_lineage, now_iso
from utils.lineage import now_iso  # noqa: E402  now_iso 只是时间工具，保留
from utils.logger import log  # noqa: E402
from utils.schemas import ConfigError, load_config_file  # noqa: E402

OPERATOR = "candidate_description"
VERSION = "v1.0.0"
STAGE_ID = "S3"          # 表1环节「数据加工」
MAIN_NAME = "09_descriptions.jsonl"
REWRITE_NAME = "09_rewrite_tasks.jsonl"
REJECT_NAME = "09_rejects.jsonl"
MODEL_INFO = {"name": None, "version": None, "degraded": False,
              "note": "规则型算子，不加载图像描述模型"}


def load_config(config_path: str | None = None, overrides: dict | None = None) -> dict:
    """加载并严格校验配置；overrides 只允许覆盖表1范围内的少量参数。"""
    return load_config_file(config_path, BASE_DIR, overrides)


def _align_text_language(record: dict, decision: dict) -> None:
    """按文本/描述的实际语言纠正 ``text.lang``，原声明值保留在 ``lang_declared``。

    上游 #04 的 ``text.lang`` 是部署声明值（默认 ``zh``），英文图注会被误标。
    这里只改与内容明显矛盾的值：以 ``text.content`` 自身的脚本判定为准，
    内容为空时才退回描述语言；判不出（``und``）时不动原值。
    """
    from utils.language import detect_language

    text = record.get("text")
    if not isinstance(text, dict):
        return
    content = text.get("content")
    detected = detect_language(content) if isinstance(content, str) and content.strip() \
        else decision.get("language")
    if not detected or detected == "und":
        return
    current = text.get("lang")
    if isinstance(current, str) and current.strip().lower() == detected:
        return
    if current is not None and "lang_declared" not in text:
        text["lang_declared"] = current
    text["lang"] = detected
    text["lang_source"] = decision.get("lang_source") or "detected"


def _build_statistics(*, total_lines: int, bad_lines: int, described: int,
                      pending: int, missing_id: int, truncated: int,
                      source_counts: dict, short: int = 0,
                      alternates_used: int = 0,
                      lang_counts: dict | None = None) -> dict:
    records = total_lines - bad_lines
    return {
        "total_lines": total_lines,
        "records": records,
        "bad_lines": bad_lines,
        "described": described,
        "pending_manual": pending,
        "missing_sample_id": missing_id,
        "rejected": bad_lines + missing_id,
        "truncated": truncated,
        "source_counts": {key: source_counts.get(key, 0) for key in SOURCE_PRIORITY},
        # 描述质量（v1.0.1）：词数不足条数、换用同源更长候选的条数、描述语言分布
        "description_short": short,
        "description_alternates_used": alternates_used,
        "description_lang_counts": dict(lang_counts or {}),
    }


def run(input_file: str, unique_id: str, config_path: str | None = None,
        output_root: str | None = None, overrides: dict | None = None) -> dict:
    """执行本算子；只写 output_root/<uniqueId>/，返回真实统计（[v4.2] 不再回传血缘）。"""
    cfg = load_config(config_path, overrides)
    started = now_iso()
    input_path = Path(input_file).resolve()
    out_root = Path(output_root).resolve() if output_root else BASE_DIR / cfg["io"]["output_root"]
    task = task_dir(out_root, unique_id)
    out_file = task / MAIN_NAME
    rewrite_file = task / REWRITE_NAME
    rej_file = task / REJECT_NAME

    if not input_path.is_file():
        log("失败", f"输入文件不存在：{input_path.name}")
        write_jsonl_atomic(out_file, [])
        write_jsonl_atomic(rewrite_file, [])
        write_jsonl_atomic(rej_file, [])
        empty_stats = _build_statistics(total_lines=0, bad_lines=0, described=0,
                                        pending=0, missing_id=0, truncated=0,
                                        source_counts={})
        # [v4.2] 血缘输出已停用：不再 build_lineage/append_lineage
        # lineage = build_lineage(OPERATOR, VERSION, unique_id, str(input_path),
        #                         str(out_file), "failed", started, now_iso(),
        #                         cfg, empty_stats, STAGE_ID, MODEL_INFO)
        # append_lineage(task, lineage)
        return {"status": "failed", "reason": "input_not_found",
                "statistics": empty_stats}

    log("输入", f"加载输入文件：{input_path.name}")

    total_lines = bad_lines = missing_id = truncated = described_cnt = pending_cnt = 0
    short_cnt = alternates_used = 0
    source_counts: dict = {}
    lang_counts: dict = {}

    # 流式读取 + 原子写出，输入输出同路径也不会截断源文件
    with AtomicJsonlWriter(out_file) as out_w, AtomicJsonlWriter(rewrite_file) as rw_w, \
            AtomicJsonlWriter(rej_file) as rj_w:
        for lineno, record, err in read_jsonl(input_path):
            total_lines += 1
            if err:
                bad_lines += 1
                rj_w.write({
                    "line": lineno,
                    "reason": err,
                    "label": "原始文件-无法解析",
                    "action": "隔离",
                    "destination": "原始隔离区",
                    "operator": OPERATOR,
                    "event_time": now_iso(),
                })
                continue
            if not record.get("sample_id"):
                missing_id += 1
                rj_w.write({
                    "line": lineno,
                    "reason": "missing_field: sample_id",
                    "label": "原始文件-无法解析",
                    "action": "隔离",
                    "destination": "原始隔离区",
                    "operator": OPERATOR,
                    "event_time": now_iso(),
                })
                continue

            decision = extract_candidate(record, cfg)
            if decision["status"] == "described":
                described_cnt += 1
                source = decision["description_source"]
                source_counts[source] = source_counts.get(source, 0) + 1
                if decision.get("quality") == "short":
                    short_cnt += 1
                if decision.get("alternate_used"):
                    alternates_used += 1
                lang_key = decision.get("language") or "und"
                lang_counts[lang_key] = lang_counts.get(lang_key, 0) + 1
                if decision["truncated"]:
                    truncated += 1
                    log("处理", f"sample_id={record['sample_id']} 描述超长已截断到 "
                                f"{decision['description_length']} 字")
                out_record = dict(record)
                out_record["description"] = decision["description"]
                out_record["description_source"] = source
                out_record["description_source_label"] = decision["description_source_label"]
                out_record["description_length"] = decision["description_length"]
                out_record["description_original_length"] = decision["original_length"]
                out_record["description_truncated"] = decision["truncated"]
                out_record["description_word_count"] = decision["word_count"]
                out_record["description_quality"] = decision["quality"]
                out_record["description_lang"] = decision["language"]
                out_record["description_lang_source"] = decision["lang_source"]
                if decision["alternate_used"]:
                    out_record["description_alternate_index"] = decision["alternate_index"]
                _align_text_language(out_record, decision)
                processing = dict(out_record.get("processing") or {})
                processing["09"] = {
                    # [v4.2] status 双字段（§2.10）：int status=1 + 原字符串迁入 process_status
                    "status": 1,
                    "process_status": "success",
                    "operator": OPERATOR,
                    "metrics": {
                        "description_source": source,
                        "description_length": decision["description_length"],
                        "truncated": decision["truncated"],
                        "word_count": decision["word_count"],
                        "quality": decision["quality"],
                        "lang": decision["language"],
                        "lang_source": decision["lang_source"],
                        "alternate_used": decision["alternate_used"],
                    },
                }
                out_record["processing"] = processing
                out_w.write(out_record)
            else:
                pending_cnt += 1
                rw_w.write(build_rewrite_task(record, decision, cfg, now_iso(), unique_id))

    statistics = _build_statistics(
        total_lines=total_lines, bad_lines=bad_lines, described=described_cnt,
        pending=pending_cnt, missing_id=missing_id, truncated=truncated,
        source_counts=source_counts, short=short_cnt,
        alternates_used=alternates_used, lang_counts=lang_counts)
    log("读取", f"读取 {total_lines} 行（坏行 {bad_lines}，缺 sample_id {missing_id}）")
    log("处理", f"有效描述 {described_cnt} 条，待人工补写 {pending_cnt} 条，截断 {truncated} 条")
    log("输出", f"写入 {out_file.name} / {rewrite_file.name} / {rej_file.name}")

    status = "success"
    # [v4.2] 血缘输出已停用：不再构建/追加 ancestry 记录
    # lineage = build_lineage(OPERATOR, VERSION, unique_id, str(input_path), str(out_file),
    #                         status, started, now_iso(), cfg, statistics, STAGE_ID, MODEL_INFO)
    # append_lineage(task, lineage)
    log("完成", f"算子执行完成，状态 {status}，输出 {out_file.name}")
    return {
        "status": status,
        "outputFile": str(out_file),
        "rewriteFile": str(rewrite_file),
        "rejectsFile": str(rej_file),
        "statistics": statistics,
        "message": f"described={described_cnt} pending_manual={pending_cnt} "
                   f"rejected={statistics['rejected']}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="算子 #09 准备候选描述")
    ap.add_argument("--input", required=True, help="跨模态语义单元 JSONL")
    ap.add_argument("--unique-id", required=True, help="任务id_文件id")
    ap.add_argument("--config", default=None, help="可选配置文件路径")
    ap.add_argument("--output-root", default=None, help="输出根目录，默认 configs 中的 output")
    args = ap.parse_args()
    try:
        result = run(args.input, args.unique_id, args.config, args.output_root)
    except (ConfigError, ValueError) as exc:
        log("失败", f"参数或配置错误：{exc}")
        sys.exit(1)
    stats = result.get("statistics", {})
    print(f"result: {result['status']} described={stats.get('described')} "
          f"pending_manual={stats.get('pending_manual')} rejected={stats.get('rejected')}")
    sys.exit(0 if result["status"] != "failed" else 1)


if __name__ == "__main__":
    main()
