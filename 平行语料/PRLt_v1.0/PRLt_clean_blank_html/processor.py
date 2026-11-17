"""算子4：空白与 HTML 清理 —— 核心处理逻辑和 CLI。

[无血缘改造 2026-10-19] 不再生成/追加血缘文件；成功/部分成功返回前打包阶段产物
artifacts/stage_04_<uniqueId>.zip 并返回 stageZip/storageUri。
"""
import argparse
import codecs
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "whitespace_html_cleanup"
OPERATOR_ID = "04"
OPERATOR_VERSION = "v1.1.0"
INVISIBLE_RE = re.compile(r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\u3164\ufeff]")
WHITESPACE_RE = re.compile(r"\s+")
HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\r\n]+")
BLOCK_TAGS = {
    "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
}
DROP_TAGS = {"script", "style", "noscript", "template"}


class _TextExtractor(HTMLParser):
    """Extract visible text from an HTML fragment without external dependencies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.html_tags_removed = 0
        self._skip_tag = None

    def _separator(self) -> None:
        if self.parts and not self.parts[-1].endswith(" "):
            self.parts.append(" ")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.html_tags_removed += 1
        if tag in DROP_TAGS:
            self._skip_tag = tag
            return
        if tag in BLOCK_TAGS:
            self._separator()

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.html_tags_removed += 1
        if tag.lower() not in DROP_TAGS and tag.lower() in BLOCK_TAGS:
            self._separator()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        self.html_tags_removed += 1
        if self._skip_tag == tag:
            self._skip_tag = None
            return
        if not self._skip_tag and tag in BLOCK_TAGS:
            self._separator()

    def handle_comment(self, data: str) -> None:
        self.html_tags_removed += 1

    def handle_data(self, data: str) -> None:
        if not self._skip_tag:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """Incrementally decode a file and yield one logical line at a time."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    buffer = ""
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk, final=False)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def select_encoding(path, encodings: List[str]) -> str:
    """Find the first configured codec that strictly decodes the complete file."""
    if not encodings:
        raise ValueError("未配置输入编码")
    errors = []
    for encoding in encodings:
        try:
            for _ in iter_decoded_lines(path, encoding):
                pass
            return encoding.lower()
        except (LookupError, OSError, UnicodeDecodeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError("输入文件无法按配置编码解码：" + ",".join(errors))


def clean_text(value: str, cfg: OperatorConfig) -> Tuple[str, Dict[str, int]]:
    """Clean one text field and return the text plus real transformation counts."""
    if not isinstance(value, str):
        raise TypeError("文本字段必须是字符串")
    counts = {
        "html_tags_removed": 0,
        "whitespace_normalized": 0,
        "invisible_chars_removed": 0,
    }
    text = value
    if cfg.strip_html:
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = parser.text()
        counts["html_tags_removed"] = parser.html_tags_removed
    if cfg.remove_invisible:
        text, counts["invisible_chars_removed"] = INVISIBLE_RE.subn("", text)
    if cfg.normalize_whitespace and cfg.whitespace_mode != "preserve":
        if cfg.whitespace_mode == "trim":
            normalized = text.strip()
            counts["whitespace_normalized"] = int(normalized != text)
        elif cfg.whitespace_mode == "linewise":
            whitespace_runs = sum(
                1 for match in HORIZONTAL_WHITESPACE_RE.finditer(text)
                if match.group(0) != " "
            )
            normalized = HORIZONTAL_WHITESPACE_RE.sub(" ", text)
            normalized = "\n".join(line.strip(" ") for line in normalized.splitlines()).strip("\n")
            if normalized != text and whitespace_runs == 0:
                whitespace_runs = 1
            counts["whitespace_normalized"] = whitespace_runs
        else:
            whitespace_runs = sum(1 for match in WHITESPACE_RE.finditer(text) if match.group(0) != " ")
            if text != text.strip():
                whitespace_runs += 1
            normalized = WHITESPACE_RE.sub(" ", text).strip()
            if normalized != text and whitespace_runs == 0:
                whitespace_runs = 1
            counts["whitespace_normalized"] = whitespace_runs
        text = normalized
    return text, counts


def _base_statistics(encoding=None) -> Dict[str, Any]:
    return {
        "total": 0,
        "passed": 0,
        "rejected": 0,
        "records_changed": 0,
        "html_tags_removed": 0,
        "whitespace_normalized": 0,
        "invisible_chars_removed": 0,
        "encoding": encoding,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _clean_record(record: dict, cfg: OperatorConfig) -> Tuple[dict, Dict[str, int], List[str]]:
    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
        record["metadata"] = metadata
    if not isinstance(metadata, dict):
        raise ValueError("invalid_type:metadata")

    totals = {"html_tags_removed": 0, "whitespace_normalized": 0, "invisible_chars_removed": 0}
    cleaned_fields = []
    changed = False
    for field in cfg.text_fields:
        if field not in record:
            continue
        if not isinstance(record[field], str):
            raise ValueError(f"invalid_type:{field}")
        original = record[field]
        cleaned, counts = clean_text(original, cfg)
        record[field] = cleaned
        for key, count in counts.items():
            totals[key] += count
        if counts["html_tags_removed"] or counts["whitespace_normalized"] or counts["invisible_chars_removed"] or cleaned != original:
            changed = True
            cleaned_fields.append(field)
    metadata["html_tags_removed"] = totals["html_tags_removed"]
    metadata["whitespace_normalized"] = totals["whitespace_normalized"]
    metadata["invisible_chars_removed"] = totals["invisible_chars_removed"]
    metadata["cleaned_fields"] = cleaned_fields
    totals["records_changed"] = int(changed)
    return record, totals, cleaned_fields


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / "04_html_cleaned.jsonl"
    rej_file = tdir / "04_html_rejects.jsonl"
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        statistics = _base_statistics()
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", now_iso(), now_iso(),
        #     {"config_error": message}, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    started = now_iso()
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"文本字段={cfg.text_fields} HTML清理={cfg.strip_html} 空白规整={cfg.normalize_whitespace}/{cfg.whitespace_mode}")
    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics()
        message = f"输入文件编码处理失败：{type(exc).__name__}，请检查文件编码"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    statistics = _base_statistics(encoding)
    log_action(logger, "读取", f"解码输入文件：{encoding}")
    try:
        with JsonlWriter(out_file) as output_writer, JsonlWriter(rej_file) as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["total"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write({"line": lineno, "record": None, "reasons": ["invalid_json"]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write({"line": lineno, "record": None, "reasons": ["invalid_record"]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue
                try:
                    cleaned, counts, _ = _clean_record(record, cfg)
                except ValueError as exc:
                    reason = str(exc)
                    reject_writer.write({"line": lineno, "record": record, "reasons": [reason]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, reason)
                    continue
                output_writer.write(cleaned)
                statistics["passed"] += 1
                statistics["records_changed"] += counts.pop("records_changed")
                for key, count in counts.items():
                    statistics[key] += count
    except (OSError, UnicodeDecodeError) as exc:
        message = f"输入文件读取失败：{type(exc).__name__}"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    if statistics["total"] == 0:
        status = "failed"
        message = "输入文件为空或无有效记录"
    elif statistics["rejected"] > 0:
        status = "partial"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", f"空白HTML清理完成：通过 {statistics['passed']} 条，拦截 {statistics['rejected']} 条，移除标签 {statistics['html_tags_removed']} 个")
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, now_iso(),
    #     cfg_snapshot, statistics))
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
    log_action(logger, "归档", f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status, "outputFile": str(out_file), "rejectFile": str(rej_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子4：空白与 HTML 清理")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
