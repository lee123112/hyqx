# -*- coding: utf-8 -*-
# [v4.2] 血缘输出已停用：本文件不再参与运行，保留存档。
"""把 canonical mty_compliance.py 同步到全部 26 个算子包。

只允许差异：每个包保留自己的 3 个打包常量行
（IS_STAGE_LAST_OPERATOR / OPERATOR_NUMBER / STAGE_ID）。
同步后校验：除这 3 行外全部包与 canonical 逐字节一致（归一化哈希组数 = 1）。
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
CONSTANTS = ("IS_STAGE_LAST_OPERATOR", "OPERATOR_NUMBER", "STAGE_ID")


def split_constants(text: str) -> tuple[dict, str]:
    """摘出 3 个常量行，返回 (常量字典, 去掉常量行后的正文)。"""
    values: dict[str, str] = {}
    body_lines: list[str] = []
    for line in text.splitlines():
        matched = None
        for name in CONSTANTS:
            if re.match(r"^%s = .+$" % name, line):
                matched = line
                break
        if matched is not None:
            name = matched.split(" = ", 1)[0]
            values[name] = matched.split(" = ", 1)[1]
        else:
            body_lines.append(line)
    return values, "\n".join(body_lines)


def main() -> int:
    canonical_path = ROOT / "CMA_package_aligned_corpus" / "utils" / "mty_compliance.py"
    canonical = io.open(canonical_path, encoding="utf-8").read()
    _canonical_values, canonical_body = split_constants(canonical)

    packages = sorted(path for path in ROOT.glob("CMA_*") if path.is_dir())
    if not packages:
        print("no packages found under", ROOT)
        return 1
    for package in packages:
        target = package / "utils" / "mty_compliance.py"
        if not target.is_file():
            print("[skip] %s (no utils/mty_compliance.py)" % package.name)
            continue
        values, _body = split_constants(io.open(target, encoding="utf-8").read())
        missing = [name for name in CONSTANTS if name not in values]
        if missing:
            print("[skip] %s (missing constants %s)" % (package.name, missing))
            continue
        # 在 canonical 全文上逐常量替换为本包取值（常量行在 canonical 里必然存在）
        rebuilt = canonical
        for name in CONSTANTS:
            pattern = re.compile(r"^%s = .+$" % name, re.M)
            rebuilt, count = pattern.subn("%s = %s" % (name, values[name]), rebuilt, count=1)
            if count != 1:
                print("[error] %s: canonical missing %s line" % (package.name, name))
                return 1
        io.open(target, "w", encoding="utf-8", newline="\n").write(rebuilt)
        print("[sync] %s (%s)" % (package.name,
                                  ", ".join("%s=%s" % (n, values[n]) for n in CONSTANTS)))

    # 校验：去掉 3 行常量后，全部包的正文与 canonical 正文一致
    digests: set[str] = set()
    ok = True
    for package in packages:
        target = package / "utils" / "mty_compliance.py"
        if not target.is_file():
            continue
        _values, body = split_constants(io.open(target, encoding="utf-8").read())
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        digests.add(digest)
    if len(digests) != 1:
        print("[FAIL] 归一化哈希组数 = %d（应为 1）" % len(digests))
        ok = False
    else:
        print("[OK] 归一化哈希组数 = 1 (%s)，共 %d 个包" % (next(iter(digests)), len(packages)))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
