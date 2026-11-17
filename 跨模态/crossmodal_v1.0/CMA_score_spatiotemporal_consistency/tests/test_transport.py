"""传输层与路径安全单测：文件内容对象、清单、zip 安全解压、媒体相对路径。"""
from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from utils import io as io_utils
from utils import transport

# [v4.2] 血缘/运输输出已停用：本文件仅存档，不再参与业务回归
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #

def test_resolve_media_rejects_unsafe_paths(tmp_path):
    for bad in ("/abs/x.json", "C:\\x.json", "media\\a.json", "http://x/a.json",
                "../escape.json", "media/../../x.json", "", "  "):
        with pytest.raises(io_utils.UnsupportedMediaPath):
            io_utils.resolve_media(tmp_path, bad)


def test_resolve_media_normalizes(tmp_path):
    target = io_utils.resolve_media(tmp_path, "./media//a.json")
    assert target == (tmp_path / "media" / "a.json").resolve()


@pytest.mark.parametrize("unique_id,ok", [
    ("task_1", True), ("task-1.v2_f1", True), ("a_f1", True), ("中文id_f1", True),
    ("", False), (" ", False), (".", False), ("..", False), ("a/../b", False),
    ("a\\b", False), ("nul", False), ("CON.txt", False), ("x:", False),
    ("trailing.", False), ("trailing ", False), ("a\x00b", False),
])
def test_validate_unique_id(unique_id, ok):
    if ok:
        assert io_utils.validate_unique_id(unique_id) == unique_id
    else:
        with pytest.raises(ValueError):
            io_utils.validate_unique_id(unique_id)


def test_task_dir_and_within(tmp_path):
    task = io_utils.task_dir(tmp_path, "task_x")
    assert task == (tmp_path / "task_x").resolve()
    assert io_utils.is_within(tmp_path.resolve(), task) is True
    with pytest.raises(ValueError):
        io_utils.task_dir(tmp_path, "../escape")


def test_resolve_in_task_blocks_escape(tmp_path):
    task = io_utils.task_dir(tmp_path, "task_y")
    (task / "input").mkdir()
    inside = task / "input" / "demo.jsonl"
    inside.write_text("{}\n", encoding="utf-8")
    assert io_utils.resolve_in_task(task, "input/demo.jsonl") == inside.resolve()
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, "../outside.jsonl")
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, "")


def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "out.json"
    io_utils.atomic_write_json(target, {"a_f1": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a_f1": 1}
    assert [path.name for path in tmp_path.iterdir()] == ["out.json"]


def test_task_lock_blocks_foreign_holder(tmp_path):
    """同进程内可重入（HTTP 层先锁、run 再锁）；外部进程持锁时必须失败。"""
    with io_utils.TaskLock(tmp_path, "task_lock"):
        with io_utils.TaskLock(tmp_path, "task_lock"):
            pass  # 同进程重入允许

    locks = tmp_path / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "task_lock.lock").write_text("999999", encoding="utf-8")  # 模拟其它进程持锁
    with pytest.raises(io_utils.TaskBusyError):
        with io_utils.TaskLock(tmp_path, "task_lock"):
            pass
    (locks / "task_lock.lock").unlink()
    with io_utils.TaskLock(tmp_path, "task_lock"):
        pass
    assert not (locks / "task_lock.lock").exists()


def test_remove_task(tmp_path):
    io_utils.task_dir(tmp_path, "task_rm")
    assert io_utils.remove_task(tmp_path, "task_rm") is True
    assert io_utils.remove_task(tmp_path, "task_rm") is False


# --------------------------------------------------------------------------- #
# 文件内容对象
# --------------------------------------------------------------------------- #

def _write_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "demo.jsonl").write_text('{"a_f1": 1}\n', encoding="utf-8")
    (run_dir / "rejects.jsonl").write_text("", encoding="utf-8")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "clip.bin").write_bytes(b"\x00\x01\x02\xff")
    return run_dir


def test_pack_and_restore_roundtrip(tmp_path):
    run_dir = _write_run_dir(tmp_path)
    payload = transport.pack_file_object(
        run_dir, "demo.jsonl", ["demo.jsonl", "rejects.jsonl", "media/clip.bin"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "demo.jsonl"
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["demo.jsonl"]["encoding"] == "utf-8"
    assert by_path["media/clip.bin"]["encoding"] == "base64"

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored)
    assert entry.read_text(encoding="utf-8") == '{"a_f1": 1}\n'
    assert (restored / "media" / "clip.bin").read_bytes() == b"\x00\x01\x02\xff"


def test_pack_rejects_missing_and_escaping_paths(tmp_path):
    run_dir = _write_run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "demo.jsonl", ["demo.jsonl", "nope.jsonl"])
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "demo.jsonl", ["../escape.jsonl"])
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "demo.jsonl", ["rejects.jsonl"])


def test_pack_rejects_binary_as_text(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "bad.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "demo.jsonl", ["demo.jsonl", "bad.json"])


def test_pack_enforces_size_limit(tmp_path):
    run_dir = _write_run_dir(tmp_path)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "demo.jsonl", ["demo.jsonl"],
                                   max_response_bytes=8)


def test_restore_rejects_bad_payloads(tmp_path):
    destination = tmp_path / "out"
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other"}, destination)
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": transport.FILE_OBJECT_FORMAT,
                                       "files": []}, destination)
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(
            {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "a.jsonl", "encoding": "base64", "content": "!!!not-base64"}]},
            destination)
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(
            {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "b.jsonl", "encoding": "utf-8", "content": "{}"}]},
            destination)
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(
            {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "a.jsonl", "encoding": "utf-16", "content": "{}"}]},
            destination)


def test_restore_rejects_duplicate_and_nonempty_destination(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dup")

    good = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
            "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}"}]}
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(good, busy)


# --------------------------------------------------------------------------- #
# 清单与入口
# --------------------------------------------------------------------------- #

def test_manifest_roundtrip_and_entrypoint(tmp_path):
    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = transport.build_manifest("demo.jsonl", [
        {"path": "demo.jsonl", "role": "main"},
        {"path": "manifest.json", "role": "ignored"},
        {"path": "rejects.jsonl", "role": "review"},
        {"path": "demo.jsonl", "role": "dup"},
    ])
    assert [item["path"] for item in manifest["artifacts"]] == ["demo.jsonl", "rejects.jsonl"]
    transport.write_manifest(tmp_path, manifest)
    loaded = transport.load_manifest(tmp_path)
    assert loaded == manifest
    entry, name = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "demo.jsonl"
    assert name == "demo.jsonl"


def test_resolve_entrypoint_errors(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"format": transport.MANIFEST_FORMAT, "entrypoint": "missing.jsonl"}),
        encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)
    (tmp_path / "manifest.json").write_text('{"format": "wrong"}', encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


# --------------------------------------------------------------------------- #
# zip 安全解压
# --------------------------------------------------------------------------- #

def test_safe_extract_zip_ok(tmp_path):
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("demo.jsonl", "{}\n")
        handle.writestr("media/a.json", "{}")
    written = io_utils.safe_extract_zip(archive, tmp_path / "out")
    assert sorted(written) == ["demo.jsonl", "media/a.json"]


@pytest.mark.parametrize("name", ["../evil.jsonl", "/abs.jsonl", ".."])
def test_safe_extract_zip_rejects_escape(tmp_path, name):
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(name, "{}\n")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "evil.jsonl").exists()


@pytest.mark.parametrize("name", ["a\\b.jsonl", "/abs.jsonl", "../x", "..", ""])
def test_normalize_zip_member_name_rejects_unsafe(name):
    """zipfile.writestr 在 Windows 会把反斜杠改写成 /，因此直接校验规范化函数。"""
    with pytest.raises(ValueError):
        io_utils.normalize_zip_member_name(name)


def test_safe_extract_zip_rejects_duplicate_case_insensitive(tmp_path):
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("A.jsonl", "{}")
        handle.writestr("a.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_rejects_symlink(tmp_path):
    archive = tmp_path / "b.zip"
    info = zipfile.ZipInfo("link.jsonl")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, "target")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_enforces_limits(tmp_path):
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a.jsonl", "{}")
        handle.writestr("b.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out", max_files=1)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out2", max_unpacked_bytes=1)
    existing = tmp_path / "busy"
    existing.mkdir()
    (existing / "x").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, existing)


def test_helpers(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    assert len(transport.file_sha256(path)) == 64
    assert transport.directory_size(tmp_path) == 5
    assert transport.is_text_path("a.jsonl") is True
    assert transport.is_media_path("a.mp4") is True
    assert transport.is_media_path("a.jsonl") is False
