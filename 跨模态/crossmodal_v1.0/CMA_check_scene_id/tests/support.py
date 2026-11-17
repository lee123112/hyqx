"""测试公共工具：读取产物、构造 zip 素材包。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

#: 会话级样例目录（由 dataset/sample/make_sample.py 生成）
MAIN_NAME = "02_scene_verified.jsonl"


def zip_directory(directory, archive) -> Path:
    """把目录打成 zip（成员使用 '/', 相对路径）。"""
    base = Path(directory)
    target = Path(archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                relative = str(path.relative_to(base)).replace("\\", "/")
                handle.write(str(path), relative)
    return target


def read_jsonl(path):
    """读取 JSONL，跳过空行，返回 dict 列表。"""
    target = Path(path)
    if not target.exists():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def count_lines(path) -> int:
    """非空行数（JSONL 条目数）。"""
    target = Path(path)
    if not target.exists():
        return 0
    return sum(1 for line in target.read_text(encoding="utf-8").splitlines() if line.strip())


def raw_lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()
