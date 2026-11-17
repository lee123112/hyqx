# -*- coding: utf-8 -*-
"""配置加载。

规则（与《情感分析算子开发规范》§6 一致）：

- 每个算子的全部阈值/窗口/门限**必须**来自 ``configs/config.yaml``，代码里不得出现魔法数字
  （单位换算常数除外，且需注释）。
- 加载优先级：环境变量 ``CONFIG_PATH`` 指定的文件 > ``<算子目录>/configs/config.yaml``。
- **fail fast**：缺键、类型错误、YAML 语法错误启动即失败，并给出明确报错；
  绝不把错误配置静默降级成默认值。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Optional

import yaml

_MISSING = object()


class ConfigError(RuntimeError):
    """配置缺失 / 类型错误 / 语法错误。"""


def load_yaml(path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError("config_not_found: %s" % p)
    try:
        with p.open("r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError("config_yaml_invalid: %s: %s" % (p.name, exc)) from exc
    if data is None:
        raise ConfigError("config_empty: %s" % p)
    if not isinstance(data, dict):
        raise ConfigError("config_not_mapping: %s" % p)
    return data


def resolve_path(op_dir, filename: str = "config.yaml") -> Path:
    """``CONFIG_PATH`` 优先，其次 ``<op_dir>/configs/config.yaml``。"""
    env = os.environ.get("CONFIG_PATH")
    if env:
        return Path(env)
    return Path(op_dir) / "configs" / filename


def load(op_dir, filename: str = "config.yaml", required_keys: Optional[list] = None) -> dict:
    """加载算子配置并做必填键校验（支持 ``a.b.c`` 点号路径）。"""
    cfg = load_yaml(resolve_path(op_dir, filename))
    for dotted in required_keys or []:
        if get(cfg, dotted, _MISSING) is _MISSING:
            raise ConfigError("config_missing_key: %s" % dotted)
    return cfg


def get(cfg: dict, dotted: str, default: Any = _MISSING) -> Any:
    """按点号路径取值；``default`` 不给则缺键报错。"""
    node: Any = cfg
    for part in str(dotted).split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise ConfigError("config_missing_key: %s" % dotted)
            return default
        node = node[part]
    return node


def typed(cfg: dict, dotted: str, expect: type, default: Any = _MISSING):
    """取值并校验类型（``bool`` 不视为 ``int``）。"""
    value = get(cfg, dotted, default)
    if value is _MISSING:
        raise ConfigError("config_missing_key: %s" % dotted)
    if expect is bool:
        if not isinstance(value, bool):
            raise ConfigError("config_type_error: %s 应为 bool，实际 %r" % (dotted, value))
        return value
    if expect is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, expect) or (expect is not bool and isinstance(value, bool)):
        raise ConfigError("config_type_error: %s 应为 %s，实际 %r" % (dotted, expect.__name__, value))
    return value


def snapshot(cfg: dict) -> dict:
    """深拷贝一份配置快照（写 ``output/model/config.json`` / 血缘 config 字段）。"""
    return copy.deepcopy(cfg)
