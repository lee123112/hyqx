"""算子22：通用同义词库替换，不调用模型。

匹配口径（按工具清单"替换词汇清单"的词级语义）：
- 拉丁字母词条按词边界匹配（\\b），不命中词内子串——"start" 不再把
  "started" 改成 "begined"，"help" 不再命中 "helpful/helpdesk"；
- 中文等无词边界词条仍按子串匹配；
- 不做词形推演，命中即整词替换，每条记录最多 1 处（一期约束）。
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.augmentation_runtime import load_operator_config, run_records

OPERATOR = "PRLt_synonym_substitution"
OPERATOR_ID = "22"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT = {"operator_version": "v1.1.0", "dictionary_path": str(Path(__file__).parent / "configs" / "general_synonyms.json"),
           "language_scope": ["source", "target"], "max_replacements_per_record": 1,
           "allow_domain_dictionary": False, "dictionary_version": "cilin-v3-m75-curated",
           "output_name": "22_synonym_substituted.jsonl", "reject_name": "22_rejects.jsonl"}


def _word_pattern(term):
    """拉丁字母词条编译词边界正则；中文等无词边界词条返回 None（走子串匹配）。

    仅当词条首尾都是字母/数字时 \\b 语义才正确；含标点边缘的词条回退子串。
    """
    if (term.isascii() and term[0].isalnum() and term[-1].isalnum()
            and any(ch.isalpha() for ch in term)):
        return re.compile(r"\b" + re.escape(term) + r"\b")
    return None


def _config(overrides=None):
    cfg = load_operator_config(BASE_DIR, DEFAULT, overrides, path_keys=("dictionary_path",))
    if cfg.get("allow_domain_dictionary"): raise ValueError("一期不支持领域同义词库")
    if not set(cfg.get("language_scope", [])) <= {"source", "target"} or not cfg.get("language_scope"):
        raise ValueError("language_scope 只能包含 source/target")
    if int(cfg.get("max_replacements_per_record", 1)) != 1: raise ValueError("一期每条最多替换 1 处")
    path = Path(cfg["dictionary_path"])
    if not path.is_file(): raise ValueError("通用同义词库不存在")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data: raise ValueError("同义词库必须为非空对象")
    cfg["dictionary"] = data
    return cfg


def run(input_file, unique_id, output_root=None, overrides=None, **_):
    try:
        cfg = _config(overrides)
        # 词边界正则在配置外预编译：cfg 会进入 config_hash（JSON 序列化），不能含正则对象
        patterns = {original: _word_pattern(original) for original in cfg["dictionary"]}

        def transform(parent, config):
            for field in config["language_scope"]:
                text = parent.get(field)
                if not isinstance(text, str): continue
                for original, replacement in config["dictionary"].items():
                    if not (isinstance(original, str) and isinstance(replacement, str)
                            and original and replacement != original):
                        continue
                    pattern = patterns.get(original)
                    if pattern is not None:
                        hit = pattern.search(text)
                        if hit is None: continue
                        changed = text[:hit.start()] + replacement + text[hit.end():]
                        match_mode = "word_boundary"
                    else:
                        if original not in text: continue
                        changed = text.replace(original, replacement, 1)
                        match_mode = "substring"
                    source = changed if field == "source" else parent.get("source")
                    target = changed if field == "target" else parent.get("target")
                    return source, target, {"dictionary_version": config["dictionary_version"],
                                            "replacement_field": field, "original": original,
                                            "replacement": replacement, "match_mode": match_mode}
            raise ValueError("未命中通用同义词")
        return run_records(input_file, unique_id, output_root or Path(__file__).parent / "output",
                           OPERATOR, "synonym_substitution", cfg, transform,
                           operator_id=OPERATOR_ID)
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None, "stageZip": None,
                "storageUri": None, "statistics": {},
                "message": f"配置或输入错误：{type(exc).__name__}: {exc}"}
