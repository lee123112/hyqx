# -*- coding: utf-8 -*-
"""26 个算子包的 mty_compliance 同步不变量 + 阶段末常量检查（规范 §3.4）。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import hashlib
import io
import re
from pathlib import Path

PACKAGE = Path(__file__).parents[1]
CONSTANTS = ("IS_STAGE_LAST_OPERATOR", "OPERATOR_NUMBER", "STAGE_ID")

#: 规范 §3.4：S1–S8 阶段末算子（M24 不是阶段末）
EXPECTED_STAGE_LAST = {3, 8, 19, 21, 22, 23, 25, 26}


def split_constants(text: str):
    values: dict = {}
    body_lines: list = []
    for line in text.splitlines():
        matched = None
        for name in CONSTANTS:
            if re.match(r"^%s = .+$" % name, line):
                matched = line
                break
        if matched is not None:
            key, value = matched.split(" = ", 1)
            values[key] = value
        else:
            body_lines.append(line)
    return values, "\n".join(body_lines)


def _packages():
    return sorted(path for path in PACKAGE.glob("CMA_*") if path.is_dir())


def test_all_26_packages_share_normalized_mty_compliance():
    digests = set()
    for package in _packages():
        target = package / "utils" / "mty_compliance.py"
        assert target.is_file(), package.name
        _values, body = split_constants(io.open(target, encoding="utf-8").read())
        digests.add(hashlib.sha256(body.encode("utf-8")).hexdigest())
    assert len(digests) == 1, "归一化哈希组数 %d（应为 1）：26 包正文必须逐字节一致" % len(digests)


def test_stage_last_constants_match_spec():
    seen_numbers = set()
    for package in _packages():
        text = io.open(package / "utils" / "mty_compliance.py", encoding="utf-8").read()
        values, _body = split_constants(text)
        number = int(values["OPERATOR_NUMBER"])
        seen_numbers.add(number)
        is_last = values["IS_STAGE_LAST_OPERATOR"] == "True"
        expect = number in EXPECTED_STAGE_LAST
        assert is_last == expect, (
            "%s: OPERATOR_NUMBER=%s IS_STAGE_LAST_OPERATOR=%s 应为 %s"
            % (package.name, number, is_last, expect))
    assert seen_numbers == set(range(1, 27)), "正式编号必须是 1..26 各一次"
