# -*- coding: utf-8 -*-
"""响应外壳、错误码与入参校验。

外层结构 ``{code, msg, data}`` 为 MTY 规范硬约束：``code=0`` 成功、``code=1`` 失败。
失败时 ``msg`` 必带机器可读前缀（前端与后端按前缀分支，不解析中文）。

错误码前缀表（全产线统一）
--------------------------
===========================  ==========================================================
前缀                          触发场景
===========================  ==========================================================
``missing_file``             未找到上传文件 / inputFile 不存在
``unknown_operator:``        ``operator`` 不在本包注册表内
``invalid_params:``          ``params`` 缺字段 / 类型错 / 枚举越界 / 数值越界
``decode_failed:``           音频无法解码（损坏、非音频、格式不支持）
``empty_audio:``             解码成功但 0 采样点
``silent_audio:``            全静音（RMS 低于阈值）
``missing_text``             音频存在但参考文本为空（入库口径）
``duplicate_uttid:``         uttid 重复（入库口径，阻断创建）
``encoding_not_utf8``        清单为 GBK/GB18030
``manifest_*``               清单缺失 / 格式不支持 / 空
``version_not_found:``       评测回传的版本 ID 不存在
``invalid_jsonl``            结果文件不是合法 JSONL
``path_escape:``             路径穿越
``zip_*``                    zip 损坏 / 条目过多 / 体积超限
``human_review_incomplete``  复核未判定完，不允许提交
``qc_not_passed:``           质检未通过，不允许发布版本
``internal_error:``          未预期异常（同时写 ERROR 日志）
===========================  ==========================================================
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence


class OpError(Exception):
    """业务失败。由服务层统一转成 ``{code:1, msg, data}``，HTTP 仍为 200。"""

    def __init__(self, msg: str, http_status: int = 200, data: Optional[dict] = None):
        super().__init__(msg)
        self.msg = msg
        self.http_status = int(http_status)
        self.data = data or {}


class ParamError(OpError):
    def __init__(self, msg: str):
        super().__init__(msg if msg.startswith("invalid_params:") else "invalid_params: %s" % msg)


# --------------------------------------------------------------------------- #
# 外壳
# --------------------------------------------------------------------------- #
def ok(data: Optional[dict] = None, msg: str = "success", logs: Optional[List[str]] = None) -> dict:
    body: Dict[str, Any] = {"code": 0, "msg": msg, "data": data or {}}
    if logs is not None:
        body["logs"] = logs
    return body


def fail(msg: str, data: Optional[dict] = None, logs: Optional[List[str]] = None) -> dict:
    body: Dict[str, Any] = {"code": 1, "msg": str(msg), "data": data or {}}
    if logs is not None:
        body["logs"] = logs
    return body


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def require(params: dict, keys: Iterable[str], where: str = "params") -> None:
    """必填校验，一次报出全部缺失键。"""
    missing = [k for k in keys if k not in params or params[k] in (None, "")]
    if missing:
        raise ParamError("%s 缺少必填字段：%s" % (where, ", ".join(missing)))


def enum(value, allowed: Sequence[Any], name: str):
    if value is None:
        raise ParamError("%s 不能为空" % name)
    if value not in allowed:
        raise ParamError("%s 取值 %r 非法，允许：%s" % (name, value, " / ".join(str(a) for a in allowed)))
    return value


def as_int(value, name: str, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ParamError("%s 必须是整数，收到 %r" % (name, value))
    if lo is not None and out < lo:
        raise ParamError("%s 不能小于 %d，收到 %d" % (name, lo, out))
    if hi is not None and out > hi:
        raise ParamError("%s 不能大于 %d，收到 %d" % (name, hi, out))
    return out


def as_float(value, name: str, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ParamError("%s 必须是数字，收到 %r" % (name, value))
    if lo is not None and out < lo:
        raise ParamError("%s 不能小于 %s，收到 %s" % (name, lo, out))
    if hi is not None and out > hi:
        raise ParamError("%s 不能大于 %s，收到 %s" % (name, hi, out))
    return out


def as_bool(value, name: str, default: Optional[bool] = None) -> bool:
    if value is None:
        if default is None:
            raise ParamError("%s 不能为空" % name)
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    raise ParamError("%s 必须是布尔值，收到 %r" % (name, value))


def as_str(value, name: str, max_len: Optional[int] = None, pattern=None) -> str:
    if value is None:
        raise ParamError("%s 不能为空" % name)
    out = str(value).strip()
    if not out:
        raise ParamError("%s 不能为空" % name)
    if max_len is not None and len(out) > max_len:
        raise ParamError("%s 长度不能超过 %d，收到 %d" % (name, max_len, len(out)))
    if pattern is not None and not pattern.match(out):
        raise ParamError("%s 格式非法：%r" % (name, out))
    return out


def as_range(value, name: str, size: int = 2) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ParamError("%s 必须是长度为 %d 的数组，收到 %r" % (name, size, value))
    out = [float(v) for v in value]
    if out[0] > out[-1]:
        raise ParamError("%s 区间下界不能大于上界：%r" % (name, value))
    return out


def as_list(value, name: str, allow_empty: bool = False) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ParamError("%s 必须是数组，收到 %r" % (name, value))
    out = list(value)
    if not out and not allow_empty:
        raise ParamError("%s 不能为空数组" % name)
    return out


def ensure_exists(path, name: str = "inputFile"):
    from pathlib import Path

    if path in (None, ""):
        raise OpError(
            "missing_file: 未提供 %s，且工作目录里也没有可用的输入文件" % name
        )
    p = Path(str(path))
    if not p.exists():
        raise OpError("missing_file: %s 不存在：%s" % (name, path))
    return p
