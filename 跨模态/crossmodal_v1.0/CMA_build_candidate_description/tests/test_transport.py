"""算子 #09 crossmodal-files-v1 文件对象与入口解析测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from utils.transport import (pack_file_object, restore_file_object,  # noqa: E402
                             resolve_entrypoint)

# [v4.2] 血缘/运输输出已停用：本文件仅存档，不再参与业务回归
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


def test_pack_and_restore_roundtrip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "09_descriptions.jsonl").write_text(
        json.dumps({"sample_id": "s1", "description": "候选描述文本"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (src / "blob.bin").write_bytes(b"\x00\x01\x02")
    payload = pack_file_object(src, "09_descriptions.jsonl",
                               ["09_descriptions.jsonl", "blob.bin"])
    assert payload["format"] == "crossmodal-files-v1"
    enc = {f["path"]: f["encoding"] for f in payload["files"]}
    assert enc["09_descriptions.jsonl"] == "utf-8"
    assert enc["blob.bin"] == "base64"

    dst = tmp_path / "dst"
    entry = restore_file_object(payload, dst)
    assert entry.read_text(encoding="utf-8").startswith('{"sample_id"')
    assert (dst / "blob.bin").read_bytes() == b"\x00\x01\x02"


def test_restore_rejects_traversal(tmp_path):
    payload = {"format": "crossmodal-files-v1", "entrypoint": "../evil.jsonl",
               "files": [{"path": "../evil.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(ValueError):
        restore_file_object(payload, tmp_path / "out")


def test_restore_rejects_bad_format(tmp_path):
    with pytest.raises(ValueError):
        restore_file_object({"format": "other", "files": []}, tmp_path / "out")


def test_resolve_entrypoint_from_manifest_and_single_jsonl(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "09_descriptions.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps(
        {"format": "crossmodal-manifest-v1", "entrypoint": "09_descriptions.jsonl"}),
        encoding="utf-8")
    entry, rel = resolve_entrypoint(d)
    assert entry.is_file() and rel == "09_descriptions.jsonl"

    d2 = tmp_path / "pkg2"
    d2.mkdir()
    (d2 / "only.jsonl").write_text("{}\n", encoding="utf-8")
    entry2, rel2 = resolve_entrypoint(d2)
    assert entry2.name == "only.jsonl"

    d3 = tmp_path / "pkg3"
    d3.mkdir()
    (d3 / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (d3 / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_entrypoint(d3)
