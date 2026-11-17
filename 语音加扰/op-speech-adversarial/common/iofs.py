# -*- coding: utf-8 -*-
"""共享文件与清单读写。

覆盖三类场景：

1. **结果文件**：JSONL / JSON 的读写，JSON 用「临时文件 + 替换」原子写。
2. **上传包**：zip 安全解压（防路径穿越 / 防 zip bomb 条目数），保留目录结构。
3. **参考文本清单**：TXT / TSV / CSV / JSONL 四种形态 + 同名 .txt 配对。

清单口径**严格**按原型研发标注《素材库 · 导入与清单说明》：

- TXT（AISHELL 风格）``BAC009S0002W0122 广州市交易会`` → 空格分隔
- LibriSpeech 风格 ``1272-128104-0000 MISTER QUILTER IS THE APOSTLE`` → 同上
- TSV/CSV（Common Voice 风格）含 ``path`` 与 ``sentence`` 两列，首行表头
- JSONL ``{"audio":"a.wav","text":"请帮我查询本月账单"}``
- 分隔符自动识别顺序：**制表符 → 逗号 → 空格**（按行内出现次数判定）
- 编码默认 UTF-8 并自动去 BOM；**检测到 GBK 直接报错要求转存 UTF-8**，不静默猜测
- 字段映射默认第 1 列为 id、第 2 列起拼接为文本；有表头时按列名猜
"""

from __future__ import annotations

import csv
import io as _io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .audio import AUDIO_EXTS

MANIFEST_EXTS = (".txt", ".tsv", ".csv", ".jsonl", ".json")
IMAGE_OR_OTHER_SKIP = (".ds_store", "thumbs.db", "__macosx")

# 表头列名猜测表（小写比较，忽略前后空格与 BOM）
ID_KEYS = ("path", "audio", "audio_path", "filename", "file", "id", "uttid", "utt", "wav")
TEXT_KEYS = ("sentence", "text", "transcript", "transcription", "label", "content", "ref")

MAX_ZIP_ENTRIES = 200000
MAX_UNCOMPRESSED = 20 * 1024 ** 3  # 20 GiB，防止 zip bomb


class ManifestError(RuntimeError):
    """参考文本清单格式 / 编码 / 内容不合法。"""


# --------------------------------------------------------------------------- #
# 目录与路径
# --------------------------------------------------------------------------- #
def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_join(base, *parts) -> Path:
    """拼接后必须仍在 base 之内，否则抛 ValueError（防路径穿越）。"""
    base_p = Path(base).resolve()
    target = base_p.joinpath(*[str(p) for p in parts]).resolve()
    if target != base_p and base_p not in target.parents:
        raise ValueError("path_escape: %s" % target)
    return target


def rel_posix(path, base) -> str:
    """产出 POSIX 风格相对路径（写 manifest / JSONL 用，避免 Windows 反斜杠）。"""
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def norm_uttid(name: str) -> str:
    """uttid 归一化：去扩展名 + 小写。全产线匹配一律用它。"""
    p = Path(str(name))
    stem = p.stem if p.suffix else p.name
    return str(stem).strip().casefold()


def _stem_lower(path) -> str:
    return Path(path).stem.casefold()


# --------------------------------------------------------------------------- #
# JSON / JSONL
# --------------------------------------------------------------------------- #
def read_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        if default is not None:
            return default
        raise FileNotFoundError("json_not_found: %s" % p)
    with p.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path, obj, indent: int = 2) -> Path:
    """原子写：先写 ``.tmp`` 再 ``os.replace``，避免半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent)
        fh.write("\n")
    os.replace(str(tmp), str(p))
    return p


def read_jsonl(path) -> List[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: List[dict] = []
    with p.open("r", encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid_jsonl: line %d: %s" % (lineno, exc)) from exc
            if not isinstance(obj, dict):
                raise ValueError("invalid_jsonl: line %d: not an object" % lineno)
            rows.append(obj)
    return rows


def write_jsonl(path, rows: Iterable[dict]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
            n += 1
    os.replace(str(tmp), str(p))
    return p


def write_text(path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(str(tmp), str(p))
    return p


# --------------------------------------------------------------------------- #
# 目录扫描 / zip
# --------------------------------------------------------------------------- #
def list_audio_files(root, exts: Sequence[str] = AUDIO_EXTS) -> List[Path]:
    """递归列出音频，按 POSIX 相对路径排序（保证结果可复现）。"""
    root_p = Path(root)
    if root_p.is_file():
        return [root_p] if root_p.suffix.lower() in exts else []
    out = [
        p
        for p in root_p.rglob("*")
        if p.is_file()
        and p.suffix.lower() in exts
        and not p.name.startswith(".")
        and "__MACOSX" not in p.parts
    ]
    return sorted(out, key=lambda p: p.relative_to(root_p).as_posix())


def list_files(root, exts: Sequence[str]) -> List[Path]:
    root_p = Path(root)
    out = [p for p in root_p.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    return sorted(out, key=lambda p: p.relative_to(root_p).as_posix())


def safe_extract_zip(zip_path, dest) -> List[Path]:
    """安全解压：拒绝绝对路径与 ``..``，限制条目数与总解压体积。"""
    dest_p = ensure_dir(dest)
    extracted: List[Path] = []
    with zipfile.ZipFile(str(zip_path)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError("zip_too_many_entries: %d" % len(infos))
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            raise ValueError("zip_too_large: %d" % total)
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            parts = [seg for seg in name.split("/") if seg not in ("", ".")]
            if not parts or any(seg == ".." for seg in parts):
                raise ValueError("zip_path_traversal: %s" % info.filename)
            if parts[0] and parts[0].lower() in IMAGE_OR_OTHER_SKIP:
                continue
            target = safe_join(dest_p, *parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted


# --------------------------------------------------------------------------- #
# 参考文本清单
# --------------------------------------------------------------------------- #
def _decode_text(path) -> Tuple[str, str]:
    """解码为文本，返回 ``(text, encoding)``。

    UTF-8（含 BOM）优先；失败后探测 GBK 并**明确报错**要求转存 UTF-8；
    仍是失败则报未知编码。不做静默替换。
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), "utf-8"
        except UnicodeDecodeError:
            continue
    for enc in ("gb18030", "gbk"):
        try:
            raw.decode(enc)
        except UnicodeDecodeError:
            continue
        raise ManifestError(
            "encoding_not_utf8: 检测到 GBK/GB18030 编码（%s），请转存为 UTF-8 后重试" % Path(path).name
        )
    raise ManifestError("encoding_unsupported: %s" % Path(path).name)


def detect_delimiter(lines: Sequence[str]) -> str:
    """按「行内出现次数」判定分隔符，优先级：制表符 > 逗号 > 空格。"""
    sample = [ln for ln in lines if ln.strip()][:50]
    if not sample:
        return "\t"
    n_tab = sum(ln.count("\t") for ln in sample)
    n_comma = sum(ln.count(",") for ln in sample)
    n_space = sum(len(re.findall(r"\s+", ln.strip())) for ln in sample)
    # 空格是兜底项：只有制表符与逗号都没有时才考虑
    candidates = [(n_tab, 3, "\t"), (n_comma, 2, ","), (n_space, 1, None)]
    n_tab_c, n_comma_c, n_space_c = n_tab, n_comma, n_space
    if n_tab_c > 0 and n_tab_c >= n_comma_c:
        return "\t"
    if n_comma_c > 0:
        return ","
    if n_space_c > 0:
        return None  # type: ignore[return-value]
    return "\t"


def _split_line(line: str, delimiter) -> List[str]:
    if delimiter is None:
        return line.split()
    if delimiter == "\t":
        return line.split("\t")
    return next(csv.reader([line], delimiter=delimiter))


def _looks_like_header(cells: Sequence[str]) -> bool:
    low = [str(c).strip().lower().lstrip("\ufeff") for c in cells]
    has_id = any(c in ID_KEYS for c in low)
    has_text = any(c in TEXT_KEYS for c in low)
    return has_id and has_text


def _guess_columns(cells: Sequence[str]) -> Tuple[int, List[int]]:
    low = [str(c).strip().lower().lstrip("\ufeff") for c in cells]
    id_idx = 0
    for i, c in enumerate(low):
        if c in ID_KEYS:
            id_idx = i
            break
    text_idx = [i for i, c in enumerate(low) if c in TEXT_KEYS]
    if not text_idx:
        text_idx = [i for i in range(len(cells)) if i != id_idx]
    return id_idx, text_idx


def parse_manifest(
    path,
    id_column: Optional[int] = None,
    text_column: Optional[Sequence[int]] = None,
    delimiter: Optional[str] = "auto",
    allow_gbk: bool = False,
) -> Tuple[Dict[str, str], dict]:
    """解析参考文本清单，返回 ``({uttid: text}, meta)``。

    ``id_column`` / ``text_column`` 为 **0 基列号**；不传则按「第 1 列 id、
    第 2 列起拼文本」的默认口径，或按表头列名猜。
    ``delimiter='auto'`` 时自动识别；显式传 ``'tab'`` / ``'comma'`` / ``'space'``
    或真实分隔符亦可。
    """
    p = Path(path)
    if not p.is_file():
        raise ManifestError("manifest_not_found: %s" % p.name)
    suffix = p.suffix.lower()
    if suffix not in MANIFEST_EXTS:
        raise ManifestError("manifest_unsupported: %s" % suffix)

    meta: dict = {"file": p.name, "format": suffix.lstrip("."), "encoding": "utf-8"}

    # --- JSONL / JSON ------------------------------------------------------- #
    if suffix in (".jsonl", ".json"):
        mapping: Dict[str, str] = {}
        duplicates: List[str] = []
        if suffix == ".json":
            text, _enc = _decode_text(p)
            payload = json.loads(text)
            rows = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            rows = read_jsonl(p)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _pick_key(row, ID_KEYS)
            val = _pick_key(row, TEXT_KEYS)
            if key is None:
                continue
            utt = norm_uttid(str(key))
            body = ("" if val is None else str(val)).strip()
            if utt in mapping:
                duplicates.append(utt)
                continue
            mapping[utt] = body
        meta.update({"delimiter": "json", "rows": len(rows), "id_column": "keys", "text_column": "keys"})
        if duplicates:
            meta["duplicate_ids"] = sorted(set(duplicates))
        return mapping, meta

    # --- TXT / TSV / CSV ---------------------------------------------------- #
    text, enc = _decode_text(p)
    meta["encoding"] = enc
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ManifestError("manifest_empty: %s" % p.name)

    if delimiter == "auto":
        delim = detect_delimiter(lines)
    elif delimiter == "tab":
        delim = "\t"
    elif delimiter == "comma":
        delim = ","
    elif delimiter == "space":
        delim = None
    else:
        delim = delimiter

    first_cells = _split_line(lines[0], delim)
    header_dropped = False
    if _looks_like_header(first_cells):
        id_idx, text_idx = _guess_columns(first_cells)
        header_dropped = True
        body_lines = lines[1:]
    else:
        id_idx = int(id_column) if id_column is not None else 0
        if text_column is None:
            text_idx = [i for i in range(len(first_cells)) if i != id_idx]
        elif isinstance(text_column, (list, tuple)):
            text_idx = [int(i) for i in text_column]
        else:
            text_idx = [int(text_column)]
        body_lines = lines

    mapping = {}
    duplicates = []
    bad_rows = 0
    for ln in body_lines:
        cells = _split_line(ln, delim)
        if len(cells) <= id_idx:
            bad_rows += 1
            continue
        utt = norm_uttid(cells[id_idx])
        parts = [cells[i].strip() for i in text_idx if 0 <= i < len(cells)]
        body = " ".join([x for x in parts if x]) if delim is not None else cells[-1].strip()
        if utt in mapping:
            duplicates.append(utt)
            continue
        mapping[utt] = body

    meta.update(
        {
            "delimiter": "\\t" if delim == "\t" else ("," if delim == "," else "space"),
            "header": header_dropped,
            "id_column": id_idx,
            "text_column": text_idx,
            "rows": len(body_lines),
            "bad_rows": bad_rows,
        }
    )
    if duplicates:
        meta["duplicate_ids"] = sorted(set(duplicates))
    return mapping, meta


def _pick_key(row: dict, keys: Sequence[str]) -> Optional[str]:
    lowered = {str(k).lower(): k for k in row.keys()}
    for k in keys:
        if k in lowered:
            return row[lowered[k]]
    return None


def parse_same_name_txt(root) -> Tuple[Dict[str, str], dict]:
    """同名 .txt 配对：``a.wav`` 旁边有 ``a.txt`` 则取其整行内容为文本。"""
    root_p = Path(root)
    mapping: Dict[str, str] = {}
    missing: List[str] = []
    for txt in sorted(root_p.rglob("*.txt")):
        if "__MACOSX" in txt.parts or txt.name.startswith("."):
            continue
        utt = _stem_lower(txt)
        if utt in mapping:
            continue
        try:
            text, _enc = _decode_text(txt)
        except ManifestError:
            continue
        body = " ".join(ln.strip() for ln in text.splitlines() if ln.strip())
        mapping[utt] = body
        if not body:
            missing.append(utt)
    return mapping, {"mode": "same_name_txt", "files": len(mapping), "empty": missing}


def normalize_text(text: str) -> str:
    """文本归一化（判重用）：压缩空白 + 去首尾。不做全角半角改写。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()
