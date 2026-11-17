"""同音容错的字符级编辑距离（离线，纯规则）。

为什么需要：表1 #13 用字符级编辑距离比例判定「音文语义一致」。Whisper-base 在中文
音频上的典型错误是**同音误写**，例如：

- `张工程师` → `張宮城師`（zhang gong cheng shi）
- `午休时` → `胡修實`（hu xiu shi）
- `锻炼身体` → `斷鏈身體`（duan lian shen ti）

这些转写的字符 CER 高达 0.56–0.57，会被判「语义不通」，但口语内容完全一致。
本模块把「替换成同音字」的代价记为 0（同音字之间不产生编辑距离），其余编辑操作
（插入/删除/非同音替换）仍按 1 计——**阈值语义不变**，只消除书写层面的同音噪声。
真实的语义差异（不同的人/地/事）不会是同音，仍会计入距离（实测最低 0.556）。

字符→音节表随包离线交付（`utils/data/PinyinTable.txt`，来源 pypinyin 0.55.0 MIT），
运行期不加载任何第三方库、不联网。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OPERATOR_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TABLE_PATH = OPERATOR_DIR / "utils" / "data" / "PinyinTable.txt"
MODEL_ROOT_TABLE = Path("pinyin") / "PinyinTable.txt"

#: 有效条目数下限：低于该值视为词典不可用（不猜、不降级伪造）
MIN_TABLE_ENTRIES = 1000

_TABLE_CACHE: Dict[str, Tuple[Dict[str, str], str, int]] = {}


class PinyinTableError(RuntimeError):
    """拼音表不可用（缺失/编码错误/条目数不足）。"""


def _candidate_paths() -> List[Path]:
    paths = [DEFAULT_TABLE_PATH]
    override = os.environ.get("MODEL_ROOT")
    if override:
        paths.append(Path(override).expanduser() / MODEL_ROOT_TABLE)
    return paths


def load_table() -> Tuple[Dict[str, str], str, int]:
    """加载字符→音节表，返回 ``(mapping, source, entries)``。

    结果按来源路径缓存；所有候选都不可用时抛 :class:`PinyinTableError`，
    由调用方把同音容错按关闭处理（显式记录，不伪造结果）。
    """
    for path in _candidate_paths():
        key = str(path)
        if key in _TABLE_CACHE:
            return _TABLE_CACHE[key]
        if not path.is_file():
            continue
        try:
            mapping: Dict[str, str] = {}
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 2 or len(parts[0]) != 1:
                        continue
                    mapping[parts[0]] = parts[1].strip().lower()
        except (OSError, UnicodeDecodeError):
            continue
        if len(mapping) < MIN_TABLE_ENTRIES:
            continue
        resolved = str(path)
        _TABLE_CACHE[key] = (mapping, resolved, len(mapping))
        return _TABLE_CACHE[key]
    raise PinyinTableError("拼音表不可用：%s" % "、".join(str(p) for p in _candidate_paths()))


def table_available() -> bool:
    try:
        load_table()
    except PinyinTableError:
        return False
    return True


def homophone_distance(transcript: Any, description: Any, table: Dict[str, str]) -> int:
    """字符级编辑距离，其中「替换成同音字」的代价为 0。

    同音判定只看无调音节是否相同；两侧任一字符不在表里时按普通替换（代价 1）处理，
    不猜读音。
    """
    left = transcript if isinstance(transcript, str) else ""
    right = description if isinstance(description, str) else ""
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char_a in enumerate(left, 1):
        current = [i] + [0] * len(right)
        syllable_a = table.get(char_a)
        for j, char_b in enumerate(right, 1):
            if char_a == char_b:
                substitution = 0
            else:
                syllable_b = table.get(char_b)
                substitution = 0 if (syllable_a and syllable_a == syllable_b) else 1
            current[j] = min(previous[j] + 1,
                             current[j - 1] + 1,
                             previous[j - 1] + substitution)
        previous = current
    return previous[-1]


def homophone_edit_ratio(transcript: Any, description: Any,
                         table: Optional[Dict[str, str]] = None) -> Optional[float]:
    """同音容错的编辑距离比例（分母为描述侧字符数，与字符 CER 口径一致）。

    任一侧为空时返回 ``None``（调用方不据此判定）。
    """
    if table is None:
        table, _source, _entries = load_table()
    if not isinstance(transcript, str) or not transcript:
        return None
    if not isinstance(description, str) or not description:
        return None
    return homophone_distance(transcript, description, table) / float(len(description))
