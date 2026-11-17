"""入口预处理层（entry adapter）：同文件双语语料 / 双语 txt → 规范化句对 JSONL。

本模块**不是算子**、不进入 `map.json`，也不调用已移出本产线的旧 00 多格式解析算子。
职责边界：只做入口输入契约归一，不做编码清洗、HTML 清理、长度过滤或质量门禁
（那些属于 02-08、10-18 算子）。

业务输入口径（用户 2026-10-22 反馈两轮合并）：

1. 同文件双语（pairs 模式，链路准入格式）：
   - ``.jsonl``：每行一个 JSON 对象，至少 source/target；兼容 id、source_lang、
     target_lang、document_id、paragraph_id、metadata；缺 id 生成确定性 ID，
     缺 document_id/paragraph_id 生成段落级标识，供 #10 消费；
   - ``.tsv`` / ``.csv``：有表头时按 source/target/id/source_lang/target_lang/
     document_id/paragraph_id 别名取列；无表头默认前两列为 source/target，
     第 3 列（如有）为 id。TSV 按 QUOTE_NONE（引号是字面字符），CSV 按标准引号；
     允许 UTF-8 BOM；
   - ``.tmx``：解析 TMX 1.4 ``<tu>/<tuv xml:lang>/<seg>``，每个 tu 一条句对；
     语言标签优先选 zh 作源、en 作目标，缺省取前两个 tuv。

2. 双语 txt（txt 模式，上游由分文件双语 PDF/TXT/Moses 转换而来）：
   - 行内分隔：TAB、`` ||| ``、`` || ``（按此优先级取第一个命中的分隔符）；
   - 或奇偶行配对（奇=source，偶=target）；
   - 配对方式混合、落单行、分隔符两侧为空时明确报错并给出样例行。

3. PDF/DOCX/HTML/XML/ZIP 等其他格式由上游处理，本层明确拒绝并提示转换口径。

规范化结果写到任务 ``input/`` 目录（``<原文件名>.normalized.jsonl``），字段与旧 00
伴随 JSONL 对齐：``id / source / target / source_lang / target_lang / metadata``，
并补充 ``document_id / paragraph_id`` 顶层字段；下游默认链为
02→…→08 → 10（把每个匹配句对作为段落输入）→ 12→…→18。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

ENTRY_ADAPTER_VERSION = "v1"
DEFAULT_SOURCE_LANG = "zh"
DEFAULT_TARGET_LANG = "en"

# 同文件双语准入格式（pairs 模式）与双语文本格式（txt 模式）。
SAME_FILE_SUFFIXES = {".jsonl", ".json", ".tsv", ".csv", ".tmx"}
TXT_SUFFIXES = {".txt"}
SUPPORTED_SUFFIXES_TEXT = ".jsonl / .tsv / .csv / .tmx（同文件双语）或 .txt（双语文本）"

# 与算子 00 旧解析配置保持一致的字段别名/分隔符口径（只参考，不复用/不恢复 00 算子）。
SOURCE_ALIASES = ("source", "src", "source_text", "src_text", "source_sentence")
TARGET_ALIASES = ("target", "tgt", "target_text", "tgt_text", "target_sentence")
ID_ALIASES = ("id", "pair_id", "uid")
SOURCE_LANG_ALIASES = ("source_lang", "srclang", "source_language", "source_locale")
TARGET_LANG_ALIASES = ("target_lang", "tgtlang", "target_language", "target_locale")
DOCUMENT_ID_ALIASES = ("document_id", "doc_id")
PARAGRAPH_ID_ALIASES = ("paragraph_id", "para_id")

# 行内分隔符：TAB 优先，其次 " ||| "，最后 " || "。
TXT_SEPARATORS: Tuple[Tuple[str, str], ...] = (
    ("\t", "TAB"),
    (" ||| ", "|||"),
    (" || ", "||"),
)

# TMX 语言标签选择：优先 zh 作源、en 作目标（相同时按文档顺序回退）。
_TMX_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_TMX_ZH_LANGS = {"zh", "zho", "chi"}
_TMX_EN_LANGS = {"en", "eng"}

csv.field_size_limit(10_000_000)

_FORMAT_LABELS = {".jsonl": "JSONL", ".json": "JSONL", ".tsv": "TSV",
                  ".csv": "CSV", ".tmx": "TMX"}


class IngestError(ValueError):
    """入口预处理错误；消息面向业务方，可直接作为 /predictChain 的错误返回。"""


def unsupported_format_message(suffix: str) -> str:
    """不支持格式的业务可读错误（提示由上游先转成同文件双语或 txt）。"""
    shown = suffix or "（无扩展名）"
    return (
        f"当前 /predictChain 入口只接受同文件双语 TSV / CSV / JSONL / TMX，或双语 txt；"
        f"收到不支持的格式 {shown}。PDF/DOCX/HTML/XML/ZIP 等请上游先转换为双语 txt"
        f"（UTF-8；TAB / \" ||| \" / \" || \" 行内分隔，或奇偶行配对），"
        f"或同文件双语 TSV/CSV/JSONL/TMX 后再提交。"
    )


def _pick(record: Dict[str, Any], aliases: Tuple[str, ...]) -> Optional[Any]:
    """按别名列表取值；键名大小写不敏感（与旧解析器的字段兼容口径一致）。"""
    lowered = {str(key).lower(): value for key, value in record.items()}
    for name in aliases:
        if name in lowered:
            return lowered[name]
    return None


def _pick_cell(row: List[str], index: Optional[int]) -> Optional[str]:
    if index is None or index < 0 or index >= len(row):
        return None
    return row[index]


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _primary_lang(value: Any) -> Optional[str]:
    """语种主标签：小写、去地区子标签（zh-CN/zh_CN→zh）；空值返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    primary = value.strip().lower().replace("_", "-").split("-")[0]
    return primary or None


def _normalize_lang(value: Any, default: str) -> str:
    return _primary_lang(value) or default


def _deterministic_pair_id(source: str, target: str) -> str:
    digest = hashlib.sha256((source + "\x1f" + target).encode("utf-8")).hexdigest()
    return f"pair-{digest[:16]}"


def _unique_id(base: str, seen: set) -> str:
    candidate = base
    suffix = 2
    while candidate in seen:
        candidate = f"{base}-r{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _sample(value: Any, limit: int = 120) -> str:
    """错误信息中的样例行：单行、长度受限、保留可辨识内容。"""
    if isinstance(value, dict):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    elif isinstance(value, (list, tuple)):
        text = repr(list(value))
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _decode_utf8(input_file: Path, data: bytes) -> str:
    """严格 UTF-8 解码（允许 BOM）；失败给出可读错误。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError(
            f"{input_file.name} 不是合法 UTF-8 文本（{exc.reason}，字节偏移 {exc.start}）；"
            "当前入口只接受 UTF-8（可带 BOM），请先转换编码后重试。"
        ) from exc


# --------------------------------------------------------------------------- #
# JSONL
# --------------------------------------------------------------------------- #
def _read_pairs_jsonl(input_file: Path, text: str) -> Tuple[List[Tuple[int, Dict[str, Any]]], Dict[str, Any]]:
    """读取同文件双语 JSONL：每行必须是 JSON 对象。"""
    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(
                f"匹配句对 JSONL 第 {line_no} 行不是合法 JSON：{exc.msg}（样例：{_sample(line)}）；"
                "每行需为一条 JSON 对象（至少含 source/target）。"
            ) from exc
        if not isinstance(value, dict):
            raise IngestError(
                f"匹配句对 JSONL 第 {line_no} 行必须是 JSON 对象，每行一条匹配句对"
                f"（样例：{_sample(line)}）；不接受 JSON 数组。"
            )
        pairs.append((line_no, value))
    if not pairs:
        raise IngestError("匹配句对 JSONL 没有有效记录（文件为空或只有空行）；每行需至少提供 source/target。")
    return pairs, {"pairing": "jsonl_lines", "separator": None, "header": None}


# --------------------------------------------------------------------------- #
# TSV / CSV
# --------------------------------------------------------------------------- #
def _index_of(cells: List[str], aliases: Tuple[str, ...]) -> Optional[int]:
    for index, cell in enumerate(cells):
        if cell in aliases:
            return index
    return None


def _detect_header(cells: List[str]) -> Optional[Dict[str, int]]:
    """识别表头：必须同时含 source 与 target 别名列，其余列按别名为可选。"""
    lowered = [str(cell).strip().lower() for cell in cells]
    source_index = _index_of(lowered, SOURCE_ALIASES)
    target_index = _index_of(lowered, TARGET_ALIASES)
    if source_index is None or target_index is None:
        return None
    mapping = {"source": source_index, "target": target_index}
    for name, aliases in (("id", ID_ALIASES),
                          ("source_lang", SOURCE_LANG_ALIASES),
                          ("target_lang", TARGET_LANG_ALIASES),
                          ("document_id", DOCUMENT_ID_ALIASES),
                          ("paragraph_id", PARAGRAPH_ID_ALIASES)):
        index = _index_of(lowered, aliases)
        if index is not None:
            mapping[name] = index
    return mapping


def _read_delimited_pairs(input_file: Path, text: str) -> Tuple[List[Tuple[int, Dict[str, Any]]], Dict[str, Any]]:
    """读取同文件双语 TSV/CSV：有表头按别名取列；无表头前两列 source/target、第 3 列可 id。"""
    suffix = input_file.suffix.lower()
    fmt_label = _FORMAT_LABELS.get(suffix, suffix)
    delimiter = "," if suffix == ".csv" else "\t"
    # TSV 按惯例引号是字面字符（QUOTE_NONE）；CSV 遵循标准引号规则。
    quoting = csv.QUOTE_MINIMAL if suffix == ".csv" else csv.QUOTE_NONE
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quoting=quoting)
    header_map: Optional[Dict[str, int]] = None
    header_cells: Optional[List[str]] = None
    header_checked = False
    pairs: List[Tuple[int, Dict[str, Any]]] = []

    for row in reader:
        line_no = reader.line_num
        if not any(str(cell).strip() for cell in row):
            continue
        if not header_checked:
            header_checked = True
            header_map = _detect_header(row)
            if header_map is not None:
                header_cells = [str(cell).strip() for cell in row]
                continue
        if header_map is not None:
            raw: Dict[str, Any] = {
                "source": _pick_cell(row, header_map.get("source")),
                "target": _pick_cell(row, header_map.get("target")),
            }
            for name in ("id", "source_lang", "target_lang", "document_id", "paragraph_id"):
                value = _pick_cell(row, header_map.get(name))
                if isinstance(value, str) and value.strip():
                    raw[name] = value.strip()
        else:
            if len(row) < 2:
                raise IngestError(
                    f"{fmt_label} 第 {line_no} 行只有 {len(row)} 列，无法构成 source/target 句对"
                    f"（样例：{_sample(row)}）；无表头时前两列必须是 source/target，"
                    "或提供 source/target 表头。"
                )
            raw = {"source": row[0], "target": row[1]}
            if len(row) >= 3 and str(row[2]).strip():
                raw["id"] = str(row[2]).strip()
        pairs.append((line_no, raw))

    if not pairs:
        raise IngestError(
            f"{fmt_label} 没有有效记录（文件为空、只有表头或只有空行）；"
            "请提供 source/target 列（有表头）或前两列逐行式句对（无表头）。")
    detail = {"pairing": f"{fmt_label.lower()}_with_header" if header_map else f"{fmt_label.lower()}_positional",
              "separator": "TAB" if delimiter == "\t" else "COMMA",
              "header": header_cells,
              "headerColumns": ({name: header_cells[index]
                                 for name, index in header_map.items()} if header_map else None)}
    return pairs, detail


# --------------------------------------------------------------------------- #
# TMX
# --------------------------------------------------------------------------- #
def _select_tmx_languages(segments: List[Dict[str, str]]) -> Tuple[int, int, str]:
    """按语言标签选源/目标：优先 zh→en；不足时回退到前两个 tuv。"""
    primary = [_primary_lang(segment.get("lang")) for segment in segments]

    def find(preferred: set) -> Optional[int]:
        for index, lang in enumerate(primary):
            if lang in preferred:
                return index
        return None

    source_index = find(_TMX_ZH_LANGS)
    target_index = find(_TMX_EN_LANGS)
    if source_index is not None and target_index is not None and source_index != target_index:
        return source_index, target_index, "zh_source_en_target"
    if source_index is not None:
        for index in range(len(segments)):
            if index != source_index:
                return source_index, index, "zh_source_first_other_target"
    if target_index is not None:
        for index in range(len(segments)):
            if index != target_index:
                return index, target_index, "first_other_source_en_target"
    return 0, 1, "first_two_tuv"


def _read_tmx_pairs(input_file: Path, text: str) -> Tuple[List[Tuple[int, Dict[str, Any]]], Dict[str, Any]]:
    """解析 TMX 1.4：每个 tu 取两个 tuv/seg 组成一条句对。"""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise IngestError(
            f"TMX 解析失败：XML 结构错误（{exc}）；请提供合法 TMX 1.4 文件"
            "（<tu>/<tuv xml:lang>/<seg> 结构）。"
        ) from exc
    if root.tag != "tmx":
        raise IngestError(f"TMX 解析失败：根节点是 <{root.tag}> 而不是 <tmx>；请确认文件为 TMX 1.4 格式。")

    header = root.find("header")
    header_srclang = header.get("srclang") if header is not None else None
    pairs: List[Tuple[int, Dict[str, Any]]] = []
    selection_counts: Dict[str, int] = {}
    for tu_index, tu in enumerate(root.iter("tu"), 1):
        segments: List[Dict[str, str]] = []
        for tuv in tu.findall("tuv"):
            lang = tuv.get(_TMX_XML_LANG) or tuv.get("lang") or ""
            seg = tuv.find("seg")
            if seg is None:
                continue
            segments.append({"lang": lang.strip(), "text": "".join(seg.itertext())})
        if len(segments) < 2:
            raise IngestError(
                f"TMX 第 {tu_index} 个 tu 只有 {len(segments)} 个有效句段（tuv/seg），"
                "无法构成源/目标句对；请检查 TMX 翻译单元是否完整。"
            )
        source_index, target_index, selection = _select_tmx_languages(segments)
        selection_counts[selection] = selection_counts.get(selection, 0) + 1
        source = segments[source_index]
        target = segments[target_index]
        raw: Dict[str, Any] = {"source": source["text"], "target": target["text"]}
        tuid = tu.get("tuid")
        if isinstance(tuid, str) and tuid.strip():
            raw["id"] = tuid.strip()
        source_lang = source["lang"]
        if not source_lang and isinstance(header_srclang, str) and header_srclang.strip() \
                and header_srclang.strip() != "*all*":
            source_lang = header_srclang.strip()
        if source_lang:
            raw["source_lang"] = source_lang
        if target["lang"]:
            raw["target_lang"] = target["lang"]
        pairs.append((tu_index, raw))

    if not pairs:
        raise IngestError("TMX 没有有效翻译单元（tu），无法构成句对；请检查文件内容。")
    detail = {"pairing": "tmx_tu", "separator": None, "header": None,
              "tuCount": len(pairs), "languageSelection": selection_counts,
              "headerSrclang": header_srclang}
    return pairs, detail


# --------------------------------------------------------------------------- #
# TXT
# --------------------------------------------------------------------------- #
def _find_separator(line: str) -> Optional[Tuple[str, str]]:
    for separator, label in TXT_SEPARATORS:
        if separator in line:
            return separator, label
    return None


def _read_txt_pairs(input_file: Path, text: str) -> Tuple[List[Tuple[int, Dict[str, Any]]], Dict[str, Any]]:
    """解析双语 txt：行内分隔优先；全部无分隔符时按奇偶行配对；混合/落单报错。"""
    entries: List[Tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        entries.append((line_no, stripped))
    if not entries:
        raise IngestError("txt 没有可解析内容（文件为空或只有空行）；请提供 UTF-8 双语文本。")

    with_separator = [(line_no, line) for line_no, line in entries if _find_separator(line) is not None]
    without_separator = [(line_no, line) for line_no, line in entries if _find_separator(line) is None]

    if with_separator and without_separator:
        missing_no, missing_line = without_separator[0]
        present_no, present_line = with_separator[0]
        present_label = _find_separator(present_line)[1]  # type: ignore[index]
        raise IngestError(
            f"txt 配对方式不一致：第 {missing_no} 行没有行内分隔符（样例：{_sample(missing_line)}），"
            f"但第 {present_no} 行使用了「{present_label}」分隔。请统一为行内分隔"
            "（TAB / \" ||| \" / \" || \"）或纯奇偶行配对。"
        )

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    if with_separator:
        separator = None
        for line_no, line in with_separator:
            found = _find_separator(line)
            assert found is not None
            separator = found
            left, right = line.split(found[0], 1)
            source, target = left.strip(), right.strip()
            if not source or not target:
                raise IngestError(
                    f"txt 第 {line_no} 行「{found[1]}」分隔符两侧存在空文本"
                    f"（样例：{_sample(line)}）；每行需同时提供源文与译文。"
                )
            pairs.append((line_no, {"source": source, "target": target}))
        detail = {"pairing": "inline", "separator": separator[1] if separator else None,
                  "valid_lines": len(entries)}
        return pairs, detail

    # 奇偶行配对：奇=source，偶=target；落单行明确报错并给出样例。
    if len(entries) % 2:
        last_no, last_line = entries[-1]
        raise IngestError(
            f"txt 奇偶行配对失败：有效行数为 {len(entries)}（奇数），第 {last_no} 行没有目标行配对"
            f"（样例：{_sample(last_line)}）。请补齐该行对应的目标句，或改用行内分隔"
            "（TAB / \" ||| \" / \" || \"）。"
        )
    for index in range(0, len(entries), 2):
        source_no, source = entries[index]
        _, target = entries[index + 1]
        pairs.append((source_no, {"source": source, "target": target}))
    detail = {"pairing": "odd_even_lines", "separator": None, "valid_lines": len(entries)}
    return pairs, detail


# --------------------------------------------------------------------------- #
# 归一与落盘
# --------------------------------------------------------------------------- #
def _normalize_pairs(raw_pairs: List[Tuple[int, Dict[str, Any]]], *, input_file: Path,
                     mode: str, source_format: str, file_digest: str,
                     pairing_detail: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把原始句对草稿归一为下游句对 JSONL 记录，并统计生成/改名数量。"""
    seen_ids: set = set()
    virtual_document_id = f"pairs-doc-{file_digest[:8]}"
    stats = {"generated_ids": 0, "renamed_ids": 0,
             "records_without_document_id": 0, "records_without_paragraph_id": 0,
             "records": len(raw_pairs)}
    rows: List[Dict[str, Any]] = []

    for seq, (record_no, raw) in enumerate(raw_pairs, 1):
        source = _clean_text(_pick(raw, SOURCE_ALIASES))
        target = _clean_text(_pick(raw, TARGET_ALIASES))
        missing = [name for name, value in (("source", source), ("target", target)) if value is None]
        if missing:
            raise IngestError(
                f"{input_file.name} 第 {record_no} 条记录缺少 {' / '.join(missing)} 文本"
                f"（样例：{_sample(raw)}）；每条匹配句对必须同时提供 source 与 target。"
            )

        raw_metadata = raw.get("metadata")
        metadata: Dict[str, Any] = {}
        if isinstance(raw_metadata, dict):
            # 过滤下划线前缀键：保留字段命名空间不接收用户数据注入（与旧解析器口径一致）。
            metadata.update({str(key): value for key, value in raw_metadata.items()
                             if not str(key).startswith("_")})

        declared_id = _pick(raw, ID_ALIASES)
        if isinstance(declared_id, str) and declared_id.strip():
            base_id = declared_id.strip()
        else:
            base_id = _deterministic_pair_id(source, target)  # type: ignore[arg-type]
            stats["generated_ids"] += 1
            if declared_id is not None:
                metadata.setdefault("declared_id", str(declared_id))
        record_id = _unique_id(base_id, seen_ids)
        if record_id != base_id:
            stats["renamed_ids"] += 1

        source_lang = _normalize_lang(_pick(raw, SOURCE_LANG_ALIASES), DEFAULT_SOURCE_LANG)
        target_lang = _normalize_lang(_pick(raw, TARGET_LANG_ALIASES), DEFAULT_TARGET_LANG)

        document_id = _pick(raw, DOCUMENT_ID_ALIASES)
        if not (isinstance(document_id, str) and document_id.strip()):
            document_id = metadata.get("document_id")
        if not (isinstance(document_id, str) and document_id.strip()):
            document_id = virtual_document_id
            stats["records_without_document_id"] += 1
            stats["virtual_document_id"] = virtual_document_id
        document_id = document_id.strip()

        paragraph_id = _pick(raw, PARAGRAPH_ID_ALIASES)
        if not (isinstance(paragraph_id, str) and paragraph_id.strip()):
            paragraph_id = metadata.get("paragraph_id")
        if not (isinstance(paragraph_id, str) and paragraph_id.strip()):
            paragraph_id = f"{document_id}-p{seq:06d}"
            stats["records_without_paragraph_id"] += 1

        metadata["document_id"] = document_id
        metadata["paragraph_id"] = paragraph_id.strip()
        metadata.setdefault("source_ref", f"{paragraph_id}-s")
        metadata.setdefault("target_ref", f"{paragraph_id}-t")
        metadata.setdefault("source_file", input_file.name)
        metadata["source_format"] = source_format
        metadata["entry_adapter"] = ENTRY_ADAPTER_VERSION
        metadata.setdefault("record_index", seq)
        if mode == "txt":
            metadata.setdefault("txt_pairing", pairing_detail.get("pairing"))
            if pairing_detail.get("separator"):
                metadata.setdefault("txt_separator", pairing_detail["separator"])
            metadata.setdefault("source_line", record_no)
        elif source_format in {"tsv", "csv"}:
            metadata.setdefault("source_line", record_no)
        elif source_format == "tmx":
            metadata.setdefault("tmx_tu_index", record_no)

        rows.append({
            "id": record_id,
            "source": source,
            "target": target,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "document_id": document_id,
            "paragraph_id": paragraph_id.strip(),
            "metadata": metadata,
        })
    return rows, stats


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def prepare_input(input_file: Path, mode: str, output_dir: Path) -> Dict[str, Any]:
    """把同文件双语语料 / 双语 txt 归一为句对 JSONL，返回准备结果摘要。

    ``mode`` 仅支持 ``pairs``（.jsonl/.tsv/.csv/.tmx）与 ``txt``（.txt）；
    ``output_dir`` 通常是任务 ``input/`` 目录。
    返回：mode / originalFile / normalizedFile / records / statistics。
    """
    input_file = Path(input_file)
    output_dir = Path(output_dir)
    suffix = input_file.suffix.lower()
    if mode == "pairs":
        if suffix not in SAME_FILE_SUFFIXES:
            raise IngestError(
                f"pairs 模式要求同文件双语格式（.jsonl / .tsv / .csv / .tmx），"
                f"当前输入：{input_file.name}。")
        source_format = "jsonl" if suffix in {".jsonl", ".json"} else suffix.lstrip(".")
    elif mode == "txt":
        if suffix not in TXT_SUFFIXES:
            raise IngestError(f"txt 模式要求 .txt 双语文本，当前输入：{input_file.name}。")
        source_format = "txt"
    else:
        raise IngestError(f"入口预处理不支持的输入模式：{mode}（仅支持 pairs / txt）。")

    data = input_file.read_bytes()
    text = _decode_utf8(input_file, data)
    file_digest = hashlib.sha256(data).hexdigest()

    if mode == "pairs":
        if source_format == "jsonl":
            raw_pairs, pairing_detail = _read_pairs_jsonl(input_file, text)
        elif source_format in {"tsv", "csv"}:
            raw_pairs, pairing_detail = _read_delimited_pairs(input_file, text)
        else:  # tmx
            raw_pairs, pairing_detail = _read_tmx_pairs(input_file, text)
    else:
        raw_pairs, pairing_detail = _read_txt_pairs(input_file, text)

    rows, normalize_stats = _normalize_pairs(
        raw_pairs, input_file=input_file, mode=mode, source_format=source_format,
        file_digest=file_digest, pairing_detail=pairing_detail)
    if not rows:
        raise IngestError(f"{input_file.name} 没有可用的匹配句对。")

    normalized_file = output_dir / f"{input_file.stem}.normalized.jsonl"
    _write_jsonl(normalized_file, rows)
    stats = {"mode": mode, "format": source_format}
    stats.update({key: value for key, value in pairing_detail.items() if key not in stats})
    stats.update(normalize_stats)
    return {
        "mode": mode,
        "originalFile": str(input_file),
        "normalizedFile": str(normalized_file),
        "records": len(rows),
        "statistics": stats,
    }
