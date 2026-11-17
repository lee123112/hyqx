"""算子 6 基础设施测试：内容对象、清单、路径安全、任务锁与流式 IO。

这些是共享契约（DEVELOPMENT_CONTRACT §3/§4/§5）中最容易被静默破坏的部分：
路径逃逸、重复成员、非空目录还原、限额旁路、任务并发。
"""
from __future__ import annotations

import base64
import json
import stat
import zipfile
from pathlib import Path

import pytest

from utils import io as io_utils
from utils import transport
from utils.transport import (
    OutputTooLargeError,
    TransportError,
    build_manifest,
    collect_payload_files,
    iter_manifest_paths,
    load_manifest,
    pack_file_object,
    resolve_entrypoint,
    restore_file_object,
    write_manifest,
)

# [v4.2] 血缘/运输输出已停用：运输类测试整体跳过（文件打包逻辑保留，由 test_api/test_operator 覆盖）
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


def _fixture_tree(root: Path) -> None:
    (root / "06_audio").mkdir(parents=True, exist_ok=True)
    (root / "media" / "s1").mkdir(parents=True, exist_ok=True)
    (root / "06_audio_index.jsonl").write_text(
        json.dumps({"sample_id": "s1", "audio": {"path": "06_audio/s1.wav"}}) + "\n",
        encoding="utf-8")
    (root / "06_rejects.jsonl").write_text("", encoding="utf-8")
    (root / "06_audio" / "s1.wav").write_bytes(WAV_BYTES)
    (root / "media" / "s1" / "raw.wav").write_bytes(WAV_BYTES)
    write_manifest(root, build_manifest("06_audio_index.jsonl", [
        {"path": "06_audio_index.jsonl", "role": "main"},
        {"path": "06_rejects.jsonl", "role": "review"},
        {"path": "06_audio/s1.wav", "role": "media"},
        {"path": "media/s1/raw.wav", "role": "media"},
    ]))


def test_pack_and_restore_roundtrip(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    _fixture_tree(source)
    manifest = load_manifest(source)
    paths = iter_manifest_paths(manifest) + ["manifest.json"]

    payload = pack_file_object(source, "06_audio_index.jsonl", paths)
    assert payload["format"] == "crossmodal-files-v1"

    entry = restore_file_object(payload, tmp_path / "restored")
    destination = tmp_path / "restored"
    assert entry.name == "06_audio_index.jsonl"
    assert (destination / "06_audio" / "s1.wav").read_bytes() == WAV_BYTES
    assert (destination / "media" / "s1" / "raw.wav").read_bytes() == WAV_BYTES

    text_item = next(item for item in payload["files"]
                     if item["path"] == "06_audio_index.jsonl")
    assert text_item["encoding"] == "utf-8"
    binary_item = next(item for item in payload["files"] if item["path"].endswith("s1.wav"))
    assert binary_item["encoding"] == "base64"


def test_pack_rejects_absolute_and_parent_paths(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TransportError):
        collect_payload_files(root, ["../outside.jsonl"])
    with pytest.raises(TransportError):
        collect_payload_files(root, [str((root / "a.jsonl").resolve())])
    with pytest.raises(TransportError):
        collect_payload_files(root, ["a\\b.jsonl"])


def test_pack_rejects_missing_file(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TransportError):
        pack_file_object(root, "a.jsonl", ["a.jsonl", "missing.bin"])


def test_pack_requires_entrypoint_in_payload(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TransportError):
        pack_file_object(root, "a.jsonl", ["b.jsonl"])


def test_pack_rejects_oversized_payload(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.jsonl").write_text("x" * 5000 + "\n", encoding="utf-8")
    with pytest.raises(OutputTooLargeError):
        pack_file_object(root, "a.jsonl", ["a.jsonl"], max_response_bytes=128)


def test_restore_rejects_wrong_format(tmp_path):
    with pytest.raises(TransportError):
        restore_file_object({"format": "other", "files": []}, tmp_path / "d")


def test_restore_rejects_non_empty_destination(tmp_path):
    destination = tmp_path / "d"
    destination.mkdir()
    (destination / "keep.txt").write_text("x", encoding="utf-8")
    payload = {"format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    with pytest.raises(TransportError):
        restore_file_object(payload, destination)


def test_restore_rejects_duplicate_and_escaping_paths(tmp_path):
    base = {"format": "crossmodal-files-v1", "entrypoint": "a.jsonl"}
    duplicated = {**base, "files": [
        {"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
        {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"},
    ]}
    with pytest.raises(TransportError):
        restore_file_object(duplicated, tmp_path / "d1")
    escaping = {**base, "files": [
        {"path": "../a.jsonl", "encoding": "utf-8", "content": "{}\n"},
    ]}
    with pytest.raises(TransportError):
        restore_file_object(escaping, tmp_path / "d2")


def test_restore_rejects_bad_base64_and_missing_entry(tmp_path):
    bad = {"format": "crossmodal-files-v1", "entrypoint": "a.bin",
           "files": [{"path": "a.bin", "encoding": "base64", "content": "not base64!!"}]}
    with pytest.raises(TransportError):
        restore_file_object(bad, tmp_path / "d1")
    missing = {"format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
               "files": [{"path": "b.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    with pytest.raises(TransportError):
        restore_file_object(missing, tmp_path / "d2")


def test_build_manifest_dedups_and_skips_manifest_itself():
    manifest = build_manifest("main.jsonl", [
        {"path": "main.jsonl", "role": "main"},
        {"path": "main.jsonl", "role": "main"},
        {"path": "manifest.json", "role": "sidecar"},
    ])
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert [item["path"] for item in manifest["artifacts"]] == ["main.jsonl"]


def test_load_manifest_rejects_wrong_format(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"format": "nope"}), encoding="utf-8")
    with pytest.raises(TransportError):
        load_manifest(tmp_path)


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"format": "crossmodal-manifest-v1", "entrypoint": "b.jsonl", "artifacts": []}),
        encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = resolve_entrypoint(tmp_path)
    assert entry.name == "b.jsonl" and relative == "b.jsonl"


def test_resolve_entrypoint_rejects_ambiguous_candidates(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TransportError):
        resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_falls_back_to_unique_candidate(tmp_path):
    (tmp_path / "only.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = resolve_entrypoint(tmp_path)
    assert entry.name == "only.jsonl" and relative == "only.jsonl"


def test_resolve_entrypoint_works_inside_dot_directory(tmp_path):
    """上传暂存目录形如 ``<root>/.tmp/upload_xxx``，隐藏文件过滤只能相对 base 判断。"""
    base = tmp_path / ".tmp" / "upload_1" / "content"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = resolve_entrypoint(base)
    assert entry.name == "demo.jsonl"
    assert relative == "sub/demo.jsonl"


def test_resolve_entrypoint_skips_hidden_files(tmp_path):
    (tmp_path / ".hidden.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    entry, _relative = resolve_entrypoint(tmp_path)
    assert entry.name == "demo.jsonl"


def test_restore_roundtrips_encoded_binary(tmp_path):
    original = bytes(range(256))
    payload = {"format": "crossmodal-files-v1", "entrypoint": "a.bin",
               "files": [{"path": "a.bin", "encoding": "base64",
                          "content": base64.b64encode(original).decode("ascii")}]}
    entry = restore_file_object(payload, tmp_path / "d")
    assert entry.read_bytes() == original


# ---------------------------------------------------------------- zip 安全

def test_safe_extract_zip_rejects_symlink(tmp_path):
    archive = tmp_path / "sym.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "target")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_rejects_parent_escape(tmp_path):
    archive = tmp_path / "esc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "boom")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_rejects_absolute_member(tmp_path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "boom")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_enforces_limits(tmp_path):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "a")
        zf.writestr("b.txt", "b")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "o1", max_files=1)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "o2", max_unpacked_bytes=1)


def test_safe_extract_zip_rejects_non_empty_target(tmp_path):
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "x").write_text("x", encoding="utf-8")
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "a")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, destination)


# ---------------------------------------------------------------- 锁 / 目录 / 流式 IO

def test_validate_unique_id_rules():
    assert io_utils.validate_unique_id("task_1") == "task_1"
    for bad in ["", " a", "a ", ".", "..", "a/b", "a\\b", "nul", "con.txt", "a:b"]:
        with pytest.raises(ValueError):
            io_utils.validate_unique_id(bad)


def test_task_lock_is_reentrant_and_released(tmp_path):
    root = io_utils.resolve_root(tmp_path / "root")
    with io_utils.TaskLock(root, "lock_a") as outer:
        inner = io_utils.TaskLock(root, "lock_a").acquire()
        inner.release()
        assert (root / ".locks" / "lock_a.lock").is_file()
        assert outer is not None
    assert not (root / ".locks" / "lock_a.lock").exists()


def test_task_lock_detects_foreign_holder(tmp_path):
    root = io_utils.resolve_root(tmp_path / "root")
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "held_f1.lock").write_text("9999", encoding="ascii")
    with pytest.raises(io_utils.TaskBusyError):
        io_utils.TaskLock(root, "held_f1").acquire()


def test_next_run_id_skips_existing(tmp_path):
    operator_dir = tmp_path / "audio_standardization"
    (operator_dir / "run001").mkdir(parents=True)
    (operator_dir / "run002").mkdir()
    (operator_dir / "notes").mkdir()
    assert io_utils.next_run_id(operator_dir) == "run003"


def test_remove_task_returns_false_for_unknown(tmp_path):
    root = tmp_path / "root"
    assert io_utils.remove_task(root, "never_f1") is False
    assert (root / ".locks" / "never.lock").exists() is False


def test_jsonl_writer_uses_lf_and_reports_count(tmp_path):
    target = tmp_path / "out.jsonl"
    with io_utils.JsonlWriter(target) as writer:
        writer.write({"a": "样例"})
        writer.write({"b": 1})
        assert writer.count == 2
    raw = target.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r\n" not in raw
    assert "样例" in raw.decode("utf-8")


def test_iter_decoded_lines_is_strict_utf8(tmp_path):
    good = tmp_path / "good.jsonl"
    good.write_text("样例\n", encoding="utf-8")
    assert list(io_utils.iter_decoded_lines(good)) == ["样例"]

    bad = tmp_path / "bad.jsonl"
    bad.write_bytes("样例".encode("gbk"))
    with pytest.raises(UnicodeDecodeError):
        io_utils.assert_utf8(bad)


def test_iter_decoded_lines_handles_crlf_and_no_trailing_newline(tmp_path):
    target = tmp_path / "crlf.jsonl"
    target.write_bytes(b"a\r\nb")
    assert list(io_utils.iter_decoded_lines(target)) == ["a", "b"]


def test_atomic_write_json_leaves_no_temp(tmp_path):
    target = tmp_path / "x.json"
    io_utils.atomic_write_json(target, {"样例": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"样例": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_resolve_media_and_task_dir_boundaries(tmp_path):
    root = io_utils.resolve_root(tmp_path / "root")
    task = io_utils.task_dir(root, "task_a")
    assert io_utils.is_within(root, task)
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(task, "http://host/a.wav")
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(task, "a\\b.wav")
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(task, "~/a.wav")


def test_resolve_in_task_rejects_outside_and_accepts_inside(tmp_path):
    root = io_utils.resolve_root(tmp_path / "root")
    task = io_utils.task_dir(root, "task_a")
    inside = task / "run001" / "x.jsonl"
    inside.parent.mkdir(parents=True)
    inside.write_text("{}\n", encoding="utf-8")
    assert io_utils.resolve_in_task(task, "run001/x.jsonl") == inside.resolve()
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, str((tmp_path / "outside.jsonl").resolve()))


def test_transport_helpers_exposed():
    assert transport.FILE_OBJECT_FORMAT == "crossmodal-files-v1"
    assert transport.MANIFEST_FORMAT == "crossmodal-manifest-v1"
    assert transport.is_media_path("a.wav") and transport.is_media_path("a.MP4")
    assert transport.is_text_path("x.jsonl")
    assert not transport.is_media_path("x.jsonl")
