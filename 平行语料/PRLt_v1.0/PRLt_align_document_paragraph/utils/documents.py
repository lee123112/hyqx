"""对齐计划加载、双语文档加载与段落切分。"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.io import iter_decoded_lines, select_encoding

# 段落以“两个或多个连续换行”分隔；允许空行内出现空格/制表符。
PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n")

PARAGRAPH_STAGE_GRANULARITY = "paragraph"


def split_paragraphs(text: str) -> Tuple[List[str], int]:
    """按空行切分段落，裁剪首尾空白；返回 (段落列表, 被丢弃的空段落数)。"""
    if not isinstance(text, str):
        raise TypeError("文档内容必须是字符串")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: List[str] = []
    empty = 0
    for chunk in PARAGRAPH_SPLIT_RE.split(normalized):
        paragraph = chunk.strip()
        if paragraph:
            paragraphs.append(paragraph)
        else:
            empty += 1
    return paragraphs, empty


def is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_document_ref(ref: str, allowed_roots: List[Path],
                         allow_absolute: bool = False) -> Path:
    """解析文档引用，越界即报错。

    相对路径必须落在允许根目录内；绝对路径只有在
    ``allow_absolute=True`` 时才被接受，否则同样要求位于允许根目录内。
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("document_ref_empty")
    roots = [Path(root).resolve() for root in allowed_roots]
    raw = Path(ref.strip())
    if raw.is_absolute():
        resolved = raw.resolve()
        in_scope = any(is_within(resolved, root) for root in roots)
        if resolved.is_file():
            if allow_absolute or in_scope:
                return resolved
            raise ValueError("path_out_of_scope:" + raw.name)
        if allow_absolute or in_scope:
            raise ValueError("document_not_found:" + raw.name)
        raise ValueError("path_out_of_scope:" + raw.name)
    in_scope_missing = False
    for root in roots:
        resolved = (root / raw).resolve()
        if not is_within(resolved, root):
            continue
        if resolved.is_file():
            return resolved
        in_scope_missing = True
    if in_scope_missing:
        raise ValueError("document_not_found:" + raw.name)
    raise ValueError("path_out_of_scope:" + raw.name)


def load_document(path: Path, encodings: List[str]) -> Tuple[List[str], str, int]:
    """严格按配置编码读取文档，返回 (段落, 实际编码, 丢弃的空段落数)。"""
    encoding = select_encoding(path, encodings)
    chunks = list(iter_decoded_lines(path, encoding))
    paragraphs, empty = split_paragraphs("\n".join(chunks))
    return paragraphs, encoding, empty


def load_plan(path: Path) -> Tuple[Optional[dict], List[str]]:
    """读取对齐计划（JSON 对象或 JSONL），返回 (计划, 错误原因列表)。"""
    reasons: List[str] = []
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return None, ["plan_empty"]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed, reasons
    if parsed is not None:
        reasons.append("plan_not_object")
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            reasons.append("plan_invalid_json:line%s" % lineno)
            continue
        if isinstance(record, dict):
            return record, reasons
        reasons.append("plan_not_object:line%s" % lineno)
    if not reasons:
        reasons.append("plan_empty")
    return None, reasons


def plan_side(plan: Dict[str, Any], side: str) -> Dict[str, Any]:
    """取出计划中的 source/target 段；缺失或类型错误时返回空字典。"""
    value = plan.get(side)
    return value if isinstance(value, dict) else {}


def plan_path_of(plan: Dict[str, Any], side: str) -> str:
    value = plan_side(plan, side).get("path")
    return value if isinstance(value, str) and value.strip() else ""


def plan_lang_of(plan: Dict[str, Any], side: str, fallback: str) -> str:
    value = plan_side(plan, side).get("lang")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def plan_includes_paragraph_stage(plan: Dict[str, Any]) -> bool:
    """计划是否路由到段落级（算子9）。

    ``stages`` 缺失时按包含处理（兼容旧计划）；显式给出阶段列表时，
    必须存在 granularity=paragraph 的阶段，否则本算子不应执行。
    """
    stages = plan.get("stages")
    if stages is None:
        return True
    if not isinstance(stages, list) or not stages:
        return False
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if stage.get("operator") == 9:
            return True
        if stage.get("granularity") == PARAGRAPH_STAGE_GRANULARITY:
            return True
    return False


__all__ = [
    "PARAGRAPH_SPLIT_RE",
    "split_paragraphs",
    "resolve_document_ref",
    "load_document",
    "load_plan",
    "plan_side",
    "plan_path_of",
    "plan_lang_of",
    "plan_includes_paragraph_stage",
]


