"""传输适配层测试：``crossmodal-files-v1`` 打包/恢复、清单与限额。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import fixtures as fx
from utils.transport import (
    FILE_OBJECT_FORMAT,
    MANIFEST_FORMAT,
    OutputTooLargeError,
    TransportError,
    build_manifest,
    pack_file_object,
    resolve_entrypoint,
    restore_file_object,
    write_manifest,
)

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


def test_pack_and_restore_round_trip_media_readable(tmp_path):
    """阶段末对象必须能恢复到全新根，并逐个打开记录引用的媒体。"""
    source = tmp_path / "input"
    fx.write_input_dir(source, [fx.make_record("s-1")])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # 模拟 run 目录：入口 JSONL + 媒体 + review 清单
    (run_dir / "10_admitted.jsonl").write_text(
        json.dumps(fx.make_record("s-1"), ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "10_return.jsonl").write_text("", encoding="utf-8")
    media = run_dir / "media"
    media.mkdir()
    (media / "img_ok.png").write_bytes(fx.png_bytes())

    payload = pack_file_object(run_dir, "10_admitted.jsonl",
                               ["10_admitted.jsonl", "10_return.jsonl", "media/img_ok.png"])
    assert payload["format"] == FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "10_admitted.jsonl"
    assert {item["path"] for item in payload["files"]} == {
        "10_admitted.jsonl", "10_return.jsonl", "media/img_ok.png"}

    restored = tmp_path / "restored"
    entry = restore_file_object(payload, restored)
    assert entry.name == "10_admitted.jsonl"
    record = json.loads(entry.read_text(encoding="utf-8").strip())
    image_path = restored / record["image"]["path"]
    assert image_path.is_file()
    assert image_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_pack_rejects_path_traversal_and_missing_entry(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "10_admitted.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(TransportError):
        pack_file_object(run_dir, "10_admitted.jsonl",
                         ["10_admitted.jsonl", "../escape.jsonl"])
    with pytest.raises(TransportError):
        pack_file_object(run_dir, "missing.jsonl", ["10_admitted.jsonl"])


def test_pack_enforces_response_limit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "10_admitted.jsonl").write_text("x" * 512, encoding="utf-8")
    with pytest.raises(OutputTooLargeError):
        pack_file_object(run_dir, "10_admitted.jsonl", ["10_admitted.jsonl"],
                         max_response_bytes=64)


def test_restore_rejects_bad_payload(tmp_path):
    with pytest.raises(TransportError):
        restore_file_object({"format": "other"}, tmp_path / "a")
    with pytest.raises(TransportError):
        restore_file_object(
            {"format": FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "../a.jsonl", "encoding": "utf-8", "content": ""}]},
            tmp_path / "b")
    with pytest.raises(TransportError):
        restore_file_object(
            {"format": FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "a.jsonl", "encoding": "base64", "content": "!!!"}]},
            tmp_path / "c")


def test_manifest_round_trip_and_entrypoint_resolution(tmp_path):
    directory = tmp_path / "input"
    fx.write_input_dir(directory, [fx.make_record("s-1")])
    manifest = build_manifest("demo.jsonl", [{"path": "demo.jsonl", "role": "main"},
                                             {"path": "media/img_ok.png", "role": "media"}])
    assert manifest["format"] == MANIFEST_FORMAT
    assert manifest["entrypoint"] == "demo.jsonl"
    write_manifest(directory, manifest)
    entry, relative = resolve_entrypoint(directory)
    assert entry.name == "demo.jsonl"
    assert relative == "demo.jsonl"


def test_resolve_entrypoint_requires_unambiguous_candidate(tmp_path):
    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "a.jsonl").write_text("", encoding="utf-8")
    (ambiguous / "b.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(TransportError):
        resolve_entrypoint(ambiguous)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TransportError):
        resolve_entrypoint(empty)
