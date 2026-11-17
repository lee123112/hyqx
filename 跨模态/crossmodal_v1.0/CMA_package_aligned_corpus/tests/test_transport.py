"""传输层测试：``crossmodal-files-v1`` 内容对象、运行清单与入口定位。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
from pathlib import Path

import pytest

from conftest import (build_input_package, isolation_entry, quality_report,
                      token_record, write_json, write_jsonl)
from utils import delivery, transport


def test_pack_and_restore_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "23_delivery.jsonl").write_text(
        json.dumps({"sample_id": "cm-0001"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "cm-0001.png").write_bytes(b"\x89PNG\x00\x01")
    (run_dir / "pkg.zip").write_bytes(b"PK\x03\x04binary")

    payload = transport.pack_file_object(
        run_dir, "23_delivery.jsonl",
        ["23_delivery.jsonl", "media/cm-0001.png", "pkg.zip"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "23_delivery.jsonl"
    encodings = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encodings["23_delivery.jsonl"] == "utf-8"
    assert encodings["media/cm-0001.png"] == "base64"
    assert encodings["pkg.zip"] == "base64"

    restored_root = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored_root)
    assert entry.name == "23_delivery.jsonl"
    assert (restored_root / "media" / "cm-0001.png").read_bytes() == b"\x89PNG\x00\x01"
    assert (restored_root / "pkg.zip").read_bytes() == b"PK\x03\x04binary"
    assert json.loads(entry.read_text(encoding="utf-8"))["sample_id"] == "cm-0001"


def test_pack_requires_entrypoint_in_files(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "a.jsonl", ["b.jsonl"])


def test_pack_deduplicates_paths(tmp_path):
    """打包侧对重复路径去重（产物清单与 manifest 常有重叠）；写回侧仍严格拒绝重复。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    payload = transport.pack_file_object(run_dir, "a.jsonl",
                                         ["a.jsonl", "a.jsonl", "a.jsonl"])
    assert len(payload["files"]) == 1
    assert payload["entrypoint"] == "a.jsonl"


def test_pack_enforces_response_limit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_bytes(b"x" * 4096)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "a.jsonl", ["a.jsonl"],
                                   max_response_bytes=128)


def test_restore_rejects_bad_format_and_escape(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "files": []}, tmp_path / "x")
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "../escape.jsonl",
               "files": [{"path": "../escape.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "y")


def test_build_manifest_skips_manifest_itself_and_duplicates():
    manifest = transport.build_manifest(
        "23_delivery.jsonl",
        [{"path": "23_delivery.jsonl", "role": "records"},
         {"path": "manifest.json", "role": "sidecar"},
         {"path": "23_delivery.jsonl", "role": "records"}])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    assert [item["path"] for item in manifest["artifacts"]] == ["23_delivery.jsonl"]


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    entry = build_input_package(tmp_path / "task", records=[token_record()],
                                materials={"isolation_list": [isolation_entry("cm-0013")]},
                                entrypoint="samples.jsonl")
    target, relative = transport.resolve_entrypoint(tmp_path / "task")
    assert target == entry.resolve() or target == entry
    assert relative == "samples.jsonl"


def test_resolve_entrypoint_ignores_material_lists(tmp_path):
    """没有清单时，隔离清单/旁路台账不能被当成业务入口。"""
    root = tmp_path / "task"
    root.mkdir()
    write_jsonl(root / "isolation_list.jsonl", [isolation_entry("cm-0013")])
    write_jsonl(root / "23_rejects.jsonl", [{"line": 1}])
    write_jsonl(root / "tokens.jsonl", [token_record()])
    target, relative = transport.resolve_entrypoint(root)
    assert relative == "tokens.jsonl"
    assert target.name == "tokens.jsonl"


def test_resolve_entrypoint_reports_ambiguity(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    write_jsonl(root / "a.jsonl", [])
    write_jsonl(root / "b.jsonl", [])
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(root)


def test_resolve_entrypoint_uses_preferred_name(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    write_jsonl(root / "zzz.jsonl", [])
    write_jsonl(root / "samples.jsonl", [])
    target, relative = transport.resolve_entrypoint(root)
    assert relative == "samples.jsonl"


def test_load_manifest_rejects_wrong_format(tmp_path):
    root = tmp_path / "task"
    write_json(root / "manifest.json", {"format": "other", "entrypoint": "a.jsonl"})
    with pytest.raises(transport.TransportError):
        transport.load_manifest(root)


def test_own_sidecars_are_not_entrypoints():
    assert set(delivery.OWN_SIDECARS).issubset(set(transport.NON_ENTRYPOINT_NAMES))
