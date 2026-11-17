"""跨模态传输契约测试（``utils/transport.py``）。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json

import pytest

from utils import io as io_utils
from utils import transport


def test_pack_file_object_inlines_text_and_marks_binary(tmp_path):
    (tmp_path / "samples.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    payload = transport.pack_file_object(tmp_path, "samples.jsonl",
                                         ["samples.jsonl", "blob.bin"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "samples.jsonl"
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["samples.jsonl"]["encoding"] == "utf-8"
    assert by_path["blob.bin"]["encoding"] == "base64"
    assert payload["totalBytes"] > 0


def test_pack_file_object_requires_entrypoint_in_files(tmp_path):
    (tmp_path / "samples.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(tmp_path, "samples.jsonl", ["other.jsonl"])


def test_pack_file_object_rejects_escaping_path(tmp_path):
    (tmp_path / "samples.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(tmp_path, "samples.jsonl",
                                   ["samples.jsonl", "../escape.jsonl"])


def test_pack_file_object_enforces_size_limit(tmp_path):
    (tmp_path / "samples.jsonl").write_bytes(b"x" * 2048)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(tmp_path, "samples.jsonl", ["samples.jsonl"],
                                   max_response_bytes=1024)


def test_restore_roundtrip_bytes_identical(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "samples.jsonl").write_text('{"样本": "中文"}\n', encoding="utf-8")
    (source / "media").mkdir()
    raw = bytes(range(256))
    (source / "media" / "a.png").write_bytes(raw)
    payload = transport.pack_file_object(source, "samples.jsonl",
                                        ["samples.jsonl", "media/a.png"])
    dest = tmp_path / "restored"
    entry = transport.restore_file_object(payload, dest)
    assert entry.name == "samples.jsonl"
    assert (dest / "media" / "a.png").read_bytes() == raw
    assert entry.read_text(encoding="utf-8") == '{"样本": "中文"}\n'


def test_restore_rejects_bad_format(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "files": []}, tmp_path)


def test_restore_rejects_duplicate_paths(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path)


def test_manifest_build_skips_itself_and_dedupes(tmp_path):
    manifest = transport.build_manifest("samples.jsonl", [
        {"path": "samples.jsonl", "role": "records"},
        {"path": "samples.jsonl", "role": "records"},
        {"path": "manifest.json", "role": "sidecar"},
        {"path": "22_rejects.jsonl", "role": "records"},
    ])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    paths = [item["path"] for item in manifest["artifacts"]]
    assert paths == ["samples.jsonl", "22_rejects.jsonl"]


def test_manifest_write_and_load_roundtrip(tmp_path):
    manifest = transport.build_manifest("samples.jsonl",
                                        [{"path": "samples.jsonl", "role": "records"}])
    transport.write_manifest(tmp_path, manifest)
    loaded = transport.load_manifest(tmp_path)
    assert transport.iter_manifest_paths(loaded) == ["samples.jsonl"]


def test_load_manifest_missing_raises(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "format": transport.MANIFEST_FORMAT, "entrypoint": "b.jsonl",
        "artifacts": [{"path": "b.jsonl", "role": "records"}]}), encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "b.jsonl"
    assert path.is_file()


def test_resolve_entrypoint_ignores_own_auxiliary_files(tmp_path):
    for name in (transport.NON_ENTRYPOINT_NAMES):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    (tmp_path / "samples.jsonl").write_text("{}\n", encoding="utf-8")
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "samples.jsonl"


def test_resolve_entrypoint_ambiguous_raises(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_prefers_known_upstream_names(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "19_candidate_pool.jsonl").write_text("{}\n", encoding="utf-8")
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "19_candidate_pool.jsonl"


def test_role_for_classifies_paths():
    assert transport.role_for("samples.jsonl") == "records"
    assert transport.role_for("media/a.png") == "media"
    assert transport.role_for("manifest.json") == "sidecar"
    assert transport.role_for("notes.txt") == "other"


def test_media_suffixes_do_not_include_jsonl():
    assert ".jsonl" not in transport.MEDIA_SUFFIXES
