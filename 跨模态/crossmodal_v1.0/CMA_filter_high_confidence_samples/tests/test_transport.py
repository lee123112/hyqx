"""传输层（``utils/transport.py``）单元测试：``crossmodal-files-v1`` 与清单。

覆盖：文本/媒体编码、entrypoint 约束、重复路径拒绝、体积上限、
清单去重、恢复时的格式与路径安全。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from utils import transport

# [v4.2] 血缘/运输输出已停用：本文件仅存档，不再参与业务回归
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


def test_pack_file_object_inlines_text_and_base64_media(tmp_path):
    run = tmp_path / "run"
    (run / "media").mkdir(parents=True)
    (run / "19_candidate_pool.jsonl").write_text(
        json.dumps({"sample_id": "s1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (run / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")

    payload = transport.pack_file_object(run, "19_candidate_pool.jsonl",
                                        ["19_candidate_pool.jsonl", "media/a.png"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "19_candidate_pool.jsonl"
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["19_candidate_pool.jsonl"]["encoding"] == "utf-8"
    assert by_path["media/a.png"]["encoding"] == "base64"
    assert by_path["media/a.png"]["role"] == "media"
    assert by_path["19_candidate_pool.jsonl"]["role"] == "records"


def test_pack_requires_entrypoint_in_files(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run, "a.jsonl", ["b.jsonl"])


def test_pack_rejects_duplicate_and_unsafe_paths(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.jsonl").write_text("{}\n", encoding="utf-8")
    payload = transport.pack_file_object(run, "a.jsonl", ["a.jsonl", "a.jsonl"])
    assert len(payload["files"]) == 1
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run, "a.jsonl", ["a.jsonl", "../escape.txt"])


def test_pack_rejects_missing_file(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run, "a.jsonl", ["a.jsonl", "nope.jsonl"])


def test_pack_respects_size_limit(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.jsonl").write_text("x" * 2048 + "\n", encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run, "a.jsonl", ["a.jsonl"], max_response_bytes=64)


def test_restore_file_object_round_trip(tmp_path):
    src = tmp_path / "src"
    (src / "media").mkdir(parents=True)
    (src / "19_candidate_pool.jsonl").write_text(
        json.dumps({"sample_id": "s1", "image": "media/a.png"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    raw = b"\x89PNG\r\n\x1a\n\x00binary-media"
    (src / "media" / "a.png").write_bytes(raw)

    payload = transport.pack_file_object(src, "19_candidate_pool.jsonl",
                                        ["19_candidate_pool.jsonl", "media/a.png"])
    dest = tmp_path / "dest"
    entry = transport.restore_file_object(payload, dest)
    assert entry.name == "19_candidate_pool.jsonl"
    assert (dest / "media" / "a.png").read_bytes() == raw
    record = json.loads(entry.read_text(encoding="utf-8").strip())
    assert record["sample_id"] == "s1"


def test_restore_rejects_wrong_format(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "entrypoint": "a.jsonl",
                                       "files": [{"path": "a.jsonl", "content": ""}]},
                                      tmp_path)


def test_restore_rejects_traversal_and_duplicates(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({
            "format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
            "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                      {"path": "../x.jsonl", "encoding": "utf-8", "content": "{}\n"}]},
            tmp_path / "d1")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({
            "format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
            "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                      {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"}]},
            tmp_path / "d2")


def test_restore_rejects_entrypoint_not_in_files(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({
            "format": transport.FILE_OBJECT_FORMAT, "entrypoint": "missing.jsonl",
            "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}]},
            tmp_path)


def test_manifest_build_dedupes_and_skips_manifest_itself():
    manifest = transport.build_manifest("19_candidate_pool.jsonl", [
        {"path": "19_candidate_pool.jsonl", "role": "records"},
        {"path": "19_candidate_pool.jsonl", "role": "records"},
        {"path": "manifest.json", "role": "sidecar"},
        {"path": "media/a.png", "role": "media"},
    ])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    paths = [item["path"] for item in manifest["artifacts"]]
    assert paths == ["19_candidate_pool.jsonl", "media/a.png"]


def test_manifest_write_and_load_round_trip(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    manifest = transport.build_manifest("19_candidate_pool.jsonl",
                                        [{"path": "19_candidate_pool.jsonl",
                                          "role": "records"}])
    transport.write_manifest(run, manifest)
    loaded = transport.load_manifest(run)
    assert loaded == manifest


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "19_candidate_pool.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "19_pending.jsonl").write_text("{}\n", encoding="utf-8")
    transport.write_manifest(run, transport.build_manifest(
        "19_candidate_pool.jsonl", [{"path": "19_candidate_pool.jsonl", "role": "records"}]))
    path, name = transport.resolve_entrypoint(run)
    assert name == "19_candidate_pool.jsonl"
    assert path.is_file()


def test_resolve_entrypoint_rejects_ambiguous_single_jsonl(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(run)


def test_resolve_entrypoint_ignores_own_sidecars(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "19_candidate_pool.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "19_pending.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "19_rejects.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "19_sample_quarantine.jsonl").write_text("{}\n", encoding="utf-8")
    path, name = transport.resolve_entrypoint(run)
    assert name == "19_candidate_pool.jsonl"


def test_role_for_classifies_known_suffixes():
    assert transport.role_for("a.jsonl") == "records"
    assert transport.role_for("a.png") == "media"
    assert transport.role_for("manifest.json") == "sidecar"
    assert transport.role_for("notes.txt") == "other"


def test_sidecar_name_list_covers_all_own_outputs():
    for name in ("19_candidate_pool.jsonl", "19_sample_quarantine.jsonl",
                 "19_pending.jsonl", "19_rejects.jsonl"):
        assert name in transport.OWN_SIDECARS
