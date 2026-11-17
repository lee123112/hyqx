"""传输适配层与路径/锁安全测试（DEVELOPMENT_CONTRACT §3、§4）。"""
from __future__ import annotations

import base64
import json
import stat
import zipfile
from pathlib import Path

import pytest

from tests.conftest import make_zip
from utils.io import (TaskBusyError, TaskLock, UnsupportedMediaPath, atomic_write_json,
                      commit_directory, is_within, remove_task, resolve_in_task,
                      resolve_media, safe_extract_zip, task_dir, validate_unique_id)
from utils.transport import (FILE_OBJECT_FORMAT, MANIFEST_FORMAT, OutputTooLargeError,
                             TransportError, build_manifest, iter_manifest_paths,
                             load_manifest, pack_file_object, resolve_entrypoint,
                             restore_file_object, write_manifest)

# [v4.2] 血缘/运输输出已停用：运输类测试整体跳过（文件打包逻辑保留，由 test_api/test_operator 覆盖）
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


# --------------------------------------------------------------------------- #
# uniqueId / 路径安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "  ", ".", "..", "a/b", "a\\b", "C:", "con", "nul.txt",
                                   "name.", "name ", "..\\escape", "sibling/../x"])
def test_validate_unique_id_rejects_unsafe(value):
    with pytest.raises(ValueError):
        validate_unique_id(value)


def test_validate_unique_id_accepts_single_directory_name():
    assert validate_unique_id("demoS2_f1") == "demoS2_f1"
    assert validate_unique_id("视频任务_01") == "视频任务_01"


def test_task_dir_is_confined_to_output_root(tmp_path):
    root = tmp_path / "root"
    task = task_dir(root, "abc_f1")
    assert task == (root.resolve() / "abc_f1")
    assert is_within(root.resolve(), task)


def test_resolve_in_task_rejects_escape_and_sibling_prefix(tmp_path):
    root = tmp_path / "root"
    task = task_dir(root, "abc_f1")
    (task / "input").mkdir(parents=True)
    (task / "input" / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    sibling = root / "abc_evil"
    sibling.mkdir(parents=True)
    (sibling / "demo.jsonl").write_text("{}\n", encoding="utf-8")

    assert resolve_in_task(task, "input/demo.jsonl") == task / "input" / "demo.jsonl"
    assert resolve_in_task(task, str(task / "input" / "demo.jsonl")).name == "demo.jsonl"

    for candidate in ("../abc_evil/demo.jsonl", str(sibling / "demo.jsonl"),
                      "../outside/demo.jsonl", ""):
        with pytest.raises(ValueError):
            resolve_in_task(task, candidate)


@pytest.mark.parametrize("value", ["/abs/media.jpg", "C:/media.jpg", "~/media.jpg",
                                   "../media.jpg", "media\\img.jpg", "http://x/y.jpg",
                                   "media/../../x.jpg"])
def test_resolve_media_rejects_unsupported_paths(tmp_path, value):
    root = tmp_path / "run"
    root.mkdir()
    with pytest.raises(UnsupportedMediaPath):
        resolve_media(root, value)


def test_resolve_media_accepts_relative_inside_root(tmp_path):
    root = tmp_path / "run"
    (root / "media").mkdir(parents=True)
    target = resolve_media(root, "media/img.jpg")
    assert target == (root / "media" / "img.jpg")
    assert is_within(root.resolve(), target)


# --------------------------------------------------------------------------- #
# 任务锁与删除隔离
# --------------------------------------------------------------------------- #

def test_task_lock_is_reentrant_in_process_and_blocks_foreign_lock(tmp_path):
    root = tmp_path / "root"
    # 同进程重入是设计行为：HTTP 层与 run() 各锁一次
    with TaskLock(root, "task_a"):
        with TaskLock(root, "task_a"):
            pass

    # 其他进程/容器留下的锁文件必须互斥
    lock_path = root / ".locks" / "task_a.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999", encoding="ascii")
    with pytest.raises(TaskBusyError):
        TaskLock(root, "task_a").acquire()
    lock_path.unlink()

    # 释放后可再次获取
    with TaskLock(root, "task_a"):
        pass


def test_remove_task_only_removes_its_own_task(tmp_path):
    root = tmp_path / "root"
    keep = task_dir(root, "keep_task")
    (keep / "input").mkdir(parents=True)
    (keep / "input" / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    doomed = task_dir(root, "doomed_task")
    (doomed / "input").mkdir(parents=True)

    assert remove_task(root, "doomed_task") is True
    assert not doomed.exists()
    assert (keep / "input" / "demo.jsonl").is_file()
    # 不存在的任务返回 False，不报错
    assert remove_task(root, "doomed_task") is False


# --------------------------------------------------------------------------- #
# zip 安全解压
# --------------------------------------------------------------------------- #

def _symlink_zip(path: Path) -> None:
    info = zipfile.ZipInfo("link.txt")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "target.txt")


def test_safe_extract_zip_rejects_traversal_symlink_and_duplicates(tmp_path):
    def extract(payload: bytes, name: str = "a.zip"):
        archive = tmp_path / name
        archive.write_bytes(payload)
        destination = tmp_path / ("out_" + name)
        return safe_extract_zip(archive, destination)

    with pytest.raises(ValueError):
        extract(make_zip({"../evil.txt": "x"}), "slip.zip")
    with pytest.raises(ValueError):
        extract(make_zip({"/abs/evil.txt": "x"}), "abs.zip")
    # 反斜杠成员名：Windows 上 zipfile 会归一化为子目录，核心不变量是绝不逃出目标目录
    archive = tmp_path / "backslash.zip"
    archive.write_bytes(make_zip({"a\\b.txt": "x"}))
    destination = tmp_path / "out_backslash"
    for relative in safe_extract_zip(archive, destination):
        assert "\\" not in relative
        assert is_within(destination.resolve(), (destination / relative).resolve())

    symlink = tmp_path / "symlink.zip"
    _symlink_zip(symlink)
    with pytest.raises(ValueError):
        safe_extract_zip(symlink, tmp_path / "out_symlink")

    # 大小写碰撞视为重复成员
    with pytest.raises(ValueError):
        extract(make_zip({"Media/Img.jpg": "a", "media/img.jpg": "b"}), "case.zip")


def test_safe_extract_zip_enforces_limits(tmp_path):
    archive = tmp_path / "many.zip"
    archive.write_bytes(make_zip({"f%d.txt" % i: "x" for i in range(5)}))
    with pytest.raises(ValueError):
        safe_extract_zip(archive, tmp_path / "limit_files", max_files=3)

    big = tmp_path / "big.zip"
    big.write_bytes(make_zip({"big.bin": b"0" * 4096}))
    with pytest.raises(ValueError):
        safe_extract_zip(big, tmp_path / "limit_bytes", max_unpacked_bytes=1024)


def test_safe_extract_zip_requires_empty_destination(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(make_zip({"a.txt": "x"}))
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        safe_extract_zip(archive, destination)


def test_safe_extract_zip_and_commit_directory(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(make_zip({"media/a.txt": "hello", "demo.jsonl": "{}\n"}))
    staging = tmp_path / "staging"
    written = safe_extract_zip(archive, staging)
    assert sorted(written) == ["demo.jsonl", "media/a.txt"]

    target = tmp_path / "input"
    commit_directory(staging, target)
    assert (target / "media" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not staging.exists()
    with pytest.raises(ValueError):
        commit_directory(tmp_path / "staging2", target)


# --------------------------------------------------------------------------- #
# manifest 与入口定位
# --------------------------------------------------------------------------- #

def test_manifest_roundtrip_and_self_exclusion(tmp_path):
    manifest = build_manifest("05_frame_index.jsonl", [
        {"path": "05_frame_index.jsonl", "role": "main"},
        {"path": "05_rejects.jsonl", "role": "review"},
        {"path": "manifest.json", "role": "sidecar"},
        {"path": "05_frame_index.jsonl", "role": "main"},
    ])
    assert manifest["format"] == MANIFEST_FORMAT
    assert [item["path"] for item in manifest["artifacts"]] == [
        "05_frame_index.jsonl", "05_rejects.jsonl"]
    write_manifest(tmp_path, manifest)
    loaded = load_manifest(tmp_path)
    assert loaded == manifest
    assert iter_manifest_paths(loaded) == ["05_frame_index.jsonl", "05_rejects.jsonl"]


def test_resolve_entrypoint_works_inside_hidden_working_directory(tmp_path):
    hidden = tmp_path / ".staging-abc"
    hidden.mkdir()
    (hidden / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    # 入口文件所在的工作目录本身是隐藏目录时，仍然必须能被定位
    entry, relative = resolve_entrypoint(hidden)
    assert entry == hidden / "demo.jsonl" and relative == "demo.jsonl"

    # 包内隐藏目录里的 JSONL 不参与入口竞争
    nested = hidden / ".git"
    nested.mkdir()
    (nested / "other.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = resolve_entrypoint(hidden)
    assert relative == "demo.jsonl"


def test_load_manifest_rejects_wrong_format(tmp_path):
    atomic_write_json(tmp_path / "manifest.json", {"format": "other", "entrypoint": "a.jsonl"})
    with pytest.raises(TransportError):
        load_manifest(tmp_path)


def test_resolve_entrypoint_variants(tmp_path):
    single = tmp_path / "single"
    single.mkdir()
    (single / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = resolve_entrypoint(single)
    assert entry.name == "demo.jsonl" and relative == "demo.jsonl"

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (ambiguous / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TransportError):
        resolve_entrypoint(ambiguous)

    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "manifest.json").write_text(
        json.dumps({"format": MANIFEST_FORMAT, "entrypoint": "nope.jsonl"}),
        encoding="utf-8")
    with pytest.raises(TransportError):
        resolve_entrypoint(missing)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TransportError):
        resolve_entrypoint(empty)


# --------------------------------------------------------------------------- #
# 阶段末文件内容对象
# --------------------------------------------------------------------------- #

def test_pack_and_restore_file_object_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "05_frames" / "s1").mkdir(parents=True)
    (run_dir / "05_frame_index.jsonl").write_text(
        json.dumps({"sample_id": "s1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "05_rejects.jsonl").write_text("", encoding="utf-8")
    (run_dir / "05_frames" / "s1" / "frame_000000_000000000.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    payload = pack_file_object(run_dir, "05_frame_index.jsonl", [
        "05_frame_index.jsonl", "05_rejects.jsonl",
        "05_frames/s1/frame_000000_000000000.jpg"], max_response_bytes=1 << 22)
    assert payload["format"] == FILE_OBJECT_FORMAT
    encodings = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encodings["05_frame_index.jsonl"] == "utf-8"
    assert encodings["05_frames/s1/frame_000000_000000000.jpg"] == "base64"

    destination = tmp_path / "restored"
    entry = restore_file_object(payload, destination)
    assert entry == destination / "05_frame_index.jsonl"
    assert entry.read_text(encoding="utf-8").strip() == '{"sample_id": "s1"}'
    assert (destination / "05_frames" / "s1" / "frame_000000_000000000.jpg").read_bytes() == b"\xff\xd8\xff\xd9"


def test_pack_file_object_fails_loudly(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "05_frame_index.jsonl").write_text("x", encoding="utf-8")
    with pytest.raises(TransportError):
        pack_file_object(run_dir, "05_frame_index.jsonl", ["missing.jsonl"])
    with pytest.raises(OutputTooLargeError):
        pack_file_object(run_dir, "05_frame_index.jsonl", ["05_frame_index.jsonl"],
                         max_response_bytes=4)
    with pytest.raises(TransportError):
        # 入口未列入 files：不能返回缺入口的内容对象
        pack_file_object(run_dir, "05_frame_index.jsonl", [])


def test_restore_file_object_rejects_invalid_payloads(tmp_path):
    with pytest.raises(TransportError):
        restore_file_object({"format": "other", "files": []}, tmp_path / "r1")
    with pytest.raises(TransportError):
        restore_file_object({"format": FILE_OBJECT_FORMAT, "files": []}, tmp_path / "r2")

    duplicate = {"format": FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl", "files": [
        {"path": "a.jsonl", "encoding": "utf-8", "content": ""},
        {"path": "A.jsonl", "encoding": "utf-8", "content": ""}]}
    with pytest.raises(TransportError):
        restore_file_object(duplicate, tmp_path / "r3")

    bad_base64 = {"format": FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl", "files": [
        {"path": "a.jsonl", "encoding": "base64", "content": "!!!not-base64!!!"}]}
    with pytest.raises(TransportError):
        restore_file_object(bad_base64, tmp_path / "r4")

    no_entry = {"format": FILE_OBJECT_FORMAT, "entrypoint": "b.jsonl", "files": [
        {"path": "a.jsonl", "encoding": "utf-8", "content": ""}]}
    with pytest.raises(TransportError):
        restore_file_object(no_entry, tmp_path / "r5")

    occupied = tmp_path / "r6"
    occupied.mkdir()
    (occupied / "already.txt").write_text("x", encoding="utf-8")
    with pytest.raises(TransportError):
        restore_file_object({"format": FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl", "files": [
            {"path": "a.jsonl", "encoding": "utf-8", "content": ""}]}, occupied)


def test_restore_rejects_absolute_and_traversal_paths(tmp_path):
    for bad in ("/abs/a.jsonl", "../a.jsonl", "dir/../../a.jsonl"):
        payload = {"format": FILE_OBJECT_FORMAT, "entrypoint": bad, "files": [
            {"path": bad, "encoding": "utf-8", "content": ""}]}
        with pytest.raises(TransportError):
            restore_file_object(payload, tmp_path / ("r_" + base64.b16encode(bad.encode()).decode()))


def test_restore_base64_payload_roundtrip(tmp_path):
    payload = {"format": FILE_OBJECT_FORMAT, "entrypoint": "a.bin", "files": [
        {"path": "a.bin", "encoding": "base64",
         "content": base64.b64encode(b"\x00\x01\x02").decode("ascii")}]}
    entry = restore_file_object(payload, tmp_path / "r")
    assert entry.read_bytes() == b"\x00\x01\x02"
