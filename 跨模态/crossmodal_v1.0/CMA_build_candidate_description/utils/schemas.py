"""算子 #09 配置模型与严格校验。

表1冻结口径：
- 默认开启、不可跳过、规则型、P0、无模型；
- 描述来源优先级固定为 图注→Alt→标题→字幕→语音转写→人工补写，只读不可重排；
- 最短长度 5（范围 5-20），最长长度 500（范围 100-500），超长截断并记录；
- 文件名和分类标签不得作为描述。
"""
from __future__ import annotations

from pathlib import Path

import yaml

# 表1冻结顺序，不允许配置改动
SOURCE_PRIORITY = ["caption", "alt", "title", "subtitle", "transcript", "manual"]
SOURCE_LABELS = {
    "caption": "图注",
    "alt": "Alt",
    "title": "标题",
    "subtitle": "字幕",
    "transcript": "语音转写",
    "manual": "人工补写",
}
MIN_LENGTH_RANGE = (5, 20)
MAX_LENGTH_RANGE = (100, 500)
DEFAULT_MIN_LENGTH = 5
DEFAULT_MAX_LENGTH = 500
# 描述词数下限（质量口径）：小于该词数标 description_quality=short，
# 并优先在同源候选图注（alt_candidates）里换一条更长的有效描述。
MIN_WORDS_RANGE = (0, 50)
DEFAULT_MIN_WORDS = 6
# 请求 config 只允许覆盖这些项，不得改动来源顺序
ALLOWED_OVERRIDE_KEYS = {"min_length", "max_length", "forbid_filename", "forbid_tags",
                         "include_transcription_payload", "min_words",
                         "prefer_longer_alternate", "detect_language"}
# [v4.2] §4.9 接口二参数命名：min_chars/max_chars/forbidden_sources 作为规范别名接受，
# 归一到内部键后再走同一套定义域校验（不改变任何算法逻辑）。
OVERRIDE_ALIASES = {"min_chars": "min_length", "max_chars": "max_length"}


class ConfigError(ValueError):
    """配置非法。"""


def _with_defaults(raw: dict) -> dict:
    raw = dict(raw or {})
    desc = dict(raw.get("description") or {})
    desc.setdefault("priority", list(SOURCE_PRIORITY))
    desc.setdefault("min_length", DEFAULT_MIN_LENGTH)
    desc.setdefault("max_length", DEFAULT_MAX_LENGTH)
    desc.setdefault("forbid_filename", True)
    desc.setdefault("forbid_tags", True)
    # 描述质量：词数下限 + 过短时优先换同源更长的候选图注
    desc.setdefault("min_words", DEFAULT_MIN_WORDS)
    desc.setdefault("prefer_longer_alternate", True)
    # 语言标记按字符脚本判定；关闭后只回显记录声明的语言，不纠正 text.lang
    desc.setdefault("detect_language", True)
    # 语音转写负载别名（#08 的 transcription.text/paragraphs）默认关闭：
    # 只有任务显式开启时才把 ASR 转写负载当作「语音转写」描述来源。
    desc.setdefault("include_transcription_payload", False)
    raw["description"] = desc
    raw.setdefault("io", {"output_root": "output"})
    return raw


def _normalize_override_keys(desc_over: dict) -> dict:
    """[v4.2] 把 §4.9 规范参数名归一到内部键（未知键保持原样，交由白名单拒绝）。"""
    normalized = {}
    for key, value in desc_over.items():
        normalized[OVERRIDE_ALIASES.get(key, key)] = value
    if "forbidden_sources" in normalized:
        sources = normalized.pop("forbidden_sources")
        if not isinstance(sources, (list, tuple)) \
                or not all(isinstance(item, str) for item in sources):
            raise ConfigError("config.description.forbidden_sources 必须是字符串数组")
        lowered = {str(item).strip().lower() for item in sources}
        normalized["forbid_filename"] = "filename" in lowered
        normalized["forbid_tags"] = "tag" in lowered
    return normalized


def apply_overrides(cfg: dict, overrides: dict | None) -> dict:
    if overrides is None:
        return cfg
    if not isinstance(overrides, dict):
        raise ConfigError("config 必须是对象")
    unknown_top = set(overrides) - {"description"}
    if unknown_top:
        raise ConfigError("config 不支持的字段：%s" % sorted(unknown_top)[0])
    desc_over = overrides.get("description", {})
    if not isinstance(desc_over, dict):
        raise ConfigError("config.description 必须是对象")
    desc_over = _normalize_override_keys(desc_over)
    unknown = set(desc_over) - ALLOWED_OVERRIDE_KEYS
    if unknown:
        raise ConfigError("config.description 不支持的字段：%s" % sorted(unknown)[0])
    cfg["description"].update(desc_over)
    return cfg


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("config 必须是对象")
    operator = raw.get("operator")
    if operator is not None and operator != "candidate_description":
        raise ConfigError("operator 必须为 candidate_description")

    desc = raw.get("description")
    if not isinstance(desc, dict):
        raise ConfigError("缺少 description 配置段")
    priority = desc.get("priority")
    if not isinstance(priority, (list, tuple)) or [str(x) for x in priority] != SOURCE_PRIORITY:
        raise ConfigError("description.priority 顺序固定为 %s，不可重排"
                          % "→".join(SOURCE_PRIORITY))

    min_len = desc.get("min_length")
    if isinstance(min_len, bool) or not isinstance(min_len, int) \
            or not (MIN_LENGTH_RANGE[0] <= min_len <= MIN_LENGTH_RANGE[1]):
        raise ConfigError("description.min_length 必须是 %d-%d 的整数" % MIN_LENGTH_RANGE)

    max_len = desc.get("max_length")
    if isinstance(max_len, bool) or not isinstance(max_len, int) \
            or not (MAX_LENGTH_RANGE[0] <= max_len <= MAX_LENGTH_RANGE[1]):
        raise ConfigError("description.max_length 必须是 %d-%d 的整数" % MAX_LENGTH_RANGE)

    if min_len > max_len:
        raise ConfigError("description.min_length 不能大于 max_length")

    for key in ("forbid_filename", "forbid_tags", "include_transcription_payload",
                "prefer_longer_alternate", "detect_language"):
        if not isinstance(desc.get(key), bool):
            raise ConfigError("description.%s 必须是布尔值" % key)

    min_words = desc.get("min_words")
    if isinstance(min_words, bool) or not isinstance(min_words, int) \
            or not (MIN_WORDS_RANGE[0] <= min_words <= MIN_WORDS_RANGE[1]):
        raise ConfigError("description.min_words 必须是 %d-%d 的整数" % MIN_WORDS_RANGE)

    io_cfg = raw.get("io")
    if not isinstance(io_cfg, dict) or not isinstance(io_cfg.get("output_root"), str) \
            or not io_cfg["output_root"].strip():
        raise ConfigError("io.output_root 必须是非空字符串")
    return raw


def load_config_file(config_path: str | None, base_dir: Path,
                     overrides: dict | None = None) -> dict:
    path = Path(config_path) if config_path else Path(base_dir) / "configs" / "config.yaml"
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("配置文件解析失败：%s" % exc) from exc
    raw = _with_defaults(raw)
    apply_overrides(raw, overrides)
    return validate_config(raw)
