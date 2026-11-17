"""传输适配层测试：文件内容对象打包/恢复、清单、路径与重复校验。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
from pathlib import Path

import pytest

from utils import transport
from utils.transport import TransportError


def _build_run_dir(root: Path) -> Path:
    run = root / "run001"
    run.mkdir(parents=True)
    (run / "21_passed.jsonl").write_text('{"sample_id": "s-1"}\n', encoding="utf-8")
    (run / "21_ledger.jsonl").write_text("", encoding="utf-8")
    media = run / "media"
    media.mkdir()
    (media / "pass_ok.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return run


def test_pack_and_restore_file_object_roundtrip(tmp_path):
    run = _build_run_dir(tmp_path)
    payload = transport.pack_file_object(run, "21_passed.jsonl",
                                        ["21_passed.jsonl", "21_ledger.jsonl",
                                         "media/pass_ok.png"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert {item["path"] for item in payload["files"]} == {
        "21_passed.jsonl", "21_ledger.jsonl", "media/pass_ok.png"}
    restored_dir = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored_dir)
    assert entry.name == "21_passed.jsonl"
    assert entry.read_text(encoding="utf-8") == '{"sample_id": "s-1"}\n'
    assert (restored_dir / "media" / "pass_ok.png").read_bytes().startswith(b"\x89PNG")


def test_pack_requires_entrypoint_in_payload(tmp_path):
    run = _build_run_dir(tmp_path)
    with pytest.raises(TransportError):
        transport.pack_file_object(run, "21_passed.jsonl", ["media/pass_ok.png"])


def test_pack_rejects_missing_file(tmp_path):
    run = _build_run_dir(tmp_path)
    with pytest.raises(TransportError):
        transport.pack_file_object(run, "21_passed.jsonl",
                                   ["21_passed.jsonl", "media/none.png"])


def test_pack_rejects_path_escape(tmp_path):
    run = _build_run_dir(tmp_path)
    with pytest.raises(TransportError):
        transport.pack_file_object(run, "21_passed.jsonl",
                                   ["21_passed.jsonl", "../outside.jsonl"])


def test_pack_enforces_response_size_limit(tmp_path):
    run = _build_run_dir(tmp_path)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run, "21_passed.jsonl", ["21_passed.jsonl"],
                                   max_response_bytes=10)


def test_file_object_rejects_duplicate_paths(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(TransportError):
        transport.restore_file_object(payload, tmp_path / "dup")


def test_file_object_rejects_traversal_and_wrong_format(tmp_path):
    bad_path = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
                "files": [{"path": "../a.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(TransportError):
        transport.restore_file_object(bad_path, tmp_path / "trav")
    with pytest.raises(TransportError):
        transport.restore_file_object({"format": "other"}, tmp_path / "fmt")


def test_manifest_roundtrip_and_entrypoint_resolution(tmp_path):
    run = _build_run_dir(tmp_path)
    manifest = transport.build_manifest("21_passed.jsonl",
                                        [{"path": "21_passed.jsonl", "role": "main"},
                                         {"path": "21_ledger.jsonl", "role": "review"},
                                         {"path": "21_passed.jsonl", "role": "main"}])
    transport.write_manifest(run, manifest)
    loaded = transport.load_manifest(run)
    assert loaded["format"] == transport.MANIFEST_FORMAT
    assert [item["path"] for item in loaded["artifacts"]] == ["21_passed.jsonl",
                                                              "21_ledger.jsonl"]
    entry, relative = transport.resolve_entrypoint(run)
    assert entry.name == "21_passed.jsonl"
    assert relative == "21_passed.jsonl"


def test_manifest_rejects_non_crossmodal_format(tmp_path):
    run = _build_run_dir(tmp_path)
    (run / "manifest.json").write_text(json.dumps({"format": "x", "entrypoint": "a.jsonl"}),
                                       encoding="utf-8")
    with pytest.raises(TransportError):
        transport.load_manifest(run)


def test_resolve_entrypoint_without_manifest_needs_unique_candidate(tmp_path):
    run = tmp_path / "plain"
    run.mkdir()
    (run / "21_passed.jsonl").write_text("", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(run)
    assert entry.name == "21_passed.jsonl"
    (run / "21_ledger.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(TransportError):
        transport.resolve_entrypoint(run)


def test_iter_manifest_paths_includes_entrypoint_first(tmp_path):
    manifest = transport.build_manifest("main.jsonl", [{"path": "a.jsonl", "role": "review"}])
    assert transport.iter_manifest_paths(manifest) == ["main.jsonl", "a.jsonl"]
