"""算子 #11 传输适配与 I/O 安全测试：crossmodal-files-v1 打包/恢复、原子写、任务锁。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
import os
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from utils import io as op_io  # noqa: E402
from utils.transport import pack_file_object, restore_file_object  # noqa: E402


def test_pack_and_restore_roundtrip(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (source / "media.bin").write_bytes(bytes(range(32)))
    payload = pack_file_object(source, "main.jsonl", ["main.jsonl", "media.bin"])
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == "main.jsonl"
    encodings = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encodings == {"main.jsonl": "utf-8", "media.bin": "base64"}

    target = tmp_path / "dst"
    entry = restore_file_object(payload, target)
    assert entry.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert (target / "media.bin").read_bytes() == bytes(range(32))


def test_restore_rejects_path_escape(tmp_path):
    payload = {"format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
               "files": [{"path": "../evil.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(ValueError):
        restore_file_object(payload, tmp_path / "dst")


def test_restore_rejects_bad_format(tmp_path):
    with pytest.raises(ValueError):
        restore_file_object({"format": "other", "files": []}, tmp_path / "dst")


def test_pack_rejects_absolute_path(tmp_path):
    with pytest.raises(ValueError):
        pack_file_object(tmp_path, "a.jsonl", [str(tmp_path / "a.jsonl")])


def test_read_jsonl_streaming_reports_line_numbers(tmp_path):
    path = tmp_path / "in.jsonl"
    path.write_text('{"a": 1}\n\n{not json}\n[1]\n{"b": 2}\n', encoding="utf-8")
    rows = list(op_io.read_jsonl(path))
    assert [line for line, _rec, _err in rows] == [1, 3, 4, 5]
    assert rows[0][1] == {"a": 1}
    assert rows[1][2] == "json_parse_error"
    assert rows[2][2] == "record_not_object"
    assert rows[3][1] == {"b": 2}


def test_read_jsonl_detects_encoding_error(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"ok": 1}\n' + b'\xff\xfe{bad}\n')
    rows = list(op_io.read_jsonl(path))
    assert rows[0][2] is None
    assert rows[1][2] == "encoding_error"


def test_atomic_write_jsonl_replaces_content(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text("stale\n", encoding="utf-8")
    op_io.atomic_write_jsonl(path, [{"sample_id": "s1"}, {"sample_id": "s2"}])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["sample_id"] == "s1"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_jsonl_refuses_same_source(tmp_path):
    path = tmp_path / "src.jsonl"
    path.write_text('{"keep": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        op_io.atomic_write_jsonl(path, [{"x": 1}], source_path=path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1}


def test_task_dir_safety(tmp_path):
    created = op_io.task_dir(tmp_path, "task_file")
    assert created.is_dir()
    for bad in ("", "..", "a/b", "a\\b", "x:y", "CON", "PRN"):
        with pytest.raises(ValueError):
            op_io.task_dir(tmp_path, bad)


def test_resolve_in_task_blocks_escape(tmp_path):
    task = tmp_path / "t"
    inside = task / "sub" / "a.jsonl"
    inside.parent.mkdir(parents=True)
    inside.write_text("{}", encoding="utf-8")
    assert op_io.resolve_in_task(task, "sub/a.jsonl") == inside.resolve()
    for bad in ("../x.jsonl", "../../x.jsonl", str(tmp_path / "outside.jsonl")):
        with pytest.raises(ValueError):
            op_io.resolve_in_task(task, bad)


def test_task_lock_excludes_concurrent_writer(tmp_path):
    with op_io.task_lock(tmp_path, "locked_f1", timeout=0):
        with pytest.raises(op_io.TaskBusy):
            with op_io.task_lock(tmp_path, "locked_f1", timeout=0):
                pass
    with op_io.task_lock(tmp_path, "locked_f1", timeout=0):
        pass
    assert not list(tmp_path.glob(".locks/*.lock"))


def test_next_run_id_increments(tmp_path):
    parent = tmp_path / "generate_candidate_pairs"
    (parent / "run001").mkdir(parents=True)
    (parent / "run002").mkdir()
    assert op_io.next_run_id(parent) == "run003"
    assert op_io.next_run_id(tmp_path / "fresh") == "run001"


def test_collect_media_paths_from_records():
    records = [
        {"sample_id": "s1", "image": "a/b.png", "audio": {"path": "c.wav"}, "video": None},
        {"sample_id": "s2", "image": {"path": "a/b.png"}, "text": {"content": "x"}},
    ]
    paths = op_io.collect_media_paths(records)
    assert paths == ["a/b.png", "c.wav"]
