"""算子 #12 传输与 IO 测试：crossmodal-files-v1 打包/恢复、路径安全、原子写。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import base64
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from conftest import png_bytes, write_jsonl  # noqa: E402
from utils import io as op_io  # noqa: E402
from utils import transport  # noqa: E402
from utils.lineage import public_node  # noqa: E402


def test_unique_id_validation():
    assert op_io.validate_unique_id(" task_a ") == "task_a"
    for bad in ["", "   ", ".", "..", "a/b", "a\\b", "C:", "CON", "a:b", "\0"]:
        with pytest.raises(ValueError):
            op_io.validate_unique_id(bad)


def test_task_dir_stays_inside_root(tmp_path):
    task = op_io.task_dir(tmp_path, "task_a")
    assert task == (tmp_path / "task_a").resolve()
    with pytest.raises(ValueError):
        op_io.task_dir(tmp_path, "../escape")


def test_resolve_in_task_accepts_inside_paths_only(tmp_path):
    task = op_io.task_dir(tmp_path, "task_a")
    (task / "input").mkdir()
    inside = task / "input" / "pairs.jsonl"
    inside.write_text("{}\n", encoding="utf-8")
    assert op_io.resolve_in_task(task, "input/pairs.jsonl") == inside.resolve()
    assert op_io.resolve_in_task(task, str(inside)) == inside.resolve()
    with pytest.raises(ValueError):
        op_io.resolve_in_task(task, "../other/pairs.jsonl")
    with pytest.raises(ValueError):
        op_io.resolve_in_task(task, str(tmp_path / "outside.jsonl"))


def test_read_jsonl_reports_errors_without_dropping(tmp_path):
    payload = b'{"a": 1}\n' + b"{broken}\n" + b"\xff\xfe bad\n" + b"[1, 2]\n" + b"\n"
    source = tmp_path / "mixed.jsonl"
    source.write_bytes(payload)
    rows = list(op_io.read_jsonl(source))
    assert [row[0] for row in rows] == [1, 2, 3, 4]
    assert rows[0][1] == {"a": 1} and rows[0][2] is None
    assert rows[1][2] == "json_parse_error"
    assert rows[2][2] == "encoding_error"
    assert rows[3][2] == "record_not_object"


def test_atomic_write_refuses_same_path(tmp_path):
    source = write_jsonl(tmp_path / "pairs.jsonl", [{"a": 1}])
    with pytest.raises(ValueError):
        op_io.atomic_write_jsonl(source, [{"a": 2}], source_path=source)
    assert json.loads(source.read_text("utf-8").strip()) == {"a": 1}


def test_atomic_write_replaces_atomically(tmp_path):
    target = tmp_path / "out.jsonl"
    assert op_io.atomic_write_jsonl(target, [{"a": 1}, {"b": 2}]) == 2
    assert not (tmp_path / "out.jsonl.tmp").exists()
    lines = target.read_text("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]


def test_task_lock_is_exclusive(tmp_path):
    with op_io.task_lock(tmp_path, "lock_case"):
        with pytest.raises(op_io.TaskBusy):
            with op_io.task_lock(tmp_path, "lock_case"):
                pass
    with op_io.task_lock(tmp_path, "lock_case"):
        pass


def test_next_run_id_increments(tmp_path):
    assert op_io.next_run_id(tmp_path) == "run001"
    (tmp_path / "run001").mkdir()
    (tmp_path / "run002").mkdir()
    assert op_io.next_run_id(tmp_path) == "run003"


def test_collect_media_paths_normalizes_and_drops_unsafe(tmp_path):
    records = [{"image": {"path": "media/a.png"}, "video": None, "audio": None},
               {"image": "media\\b.png", "video": {"frames": [{"path": "frames/1.png"}]},
                "audio": None},
               {"image": "../escape.png"}, {"image": "C:/abs.png"}]
    found = op_io.collect_media_paths(records)
    assert found == ["frames/1.png", "media/a.png", "media/b.png"]


def test_copy_media_reports_missing_without_faking(tmp_path):
    media_root = tmp_path / "src"
    (media_root / "media").mkdir(parents=True)
    (media_root / "media" / "a.png").write_bytes(png_bytes())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    carried, missing = op_io.copy_media(run_dir, media_root, ["media/a.png", "media/gone.png"])
    assert carried == ["media/a.png"]
    assert missing == ["media/gone.png"]
    assert (run_dir / "media" / "a.png").is_file()
    assert not (run_dir / "media" / "gone.png").exists()


def test_pack_and_restore_file_object_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "12_image_text_scores.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (run_dir / "12_isolation.jsonl").write_text("", encoding="utf-8")
    (run_dir / "media").mkdir()
    payload = png_bytes()
    (run_dir / "media" / "a.png").write_bytes(payload)

    files = transport.pack_file_object(
        run_dir, "12_image_text_scores.jsonl",
        ["12_image_text_scores.jsonl", "12_isolation.jsonl", "media/a.png"])
    assert files["format"] == "crossmodal-files-v1"
    assert files["entrypoint"] == "12_image_text_scores.jsonl"
    entries = {item["path"]: item for item in files["files"]}
    assert entries["media/a.png"]["encoding"] == "base64"
    assert base64.b64decode(entries["media/a.png"]["content"]) == payload

    restore_dir = tmp_path / "restore"
    entry = transport.restore_file_object(files, restore_dir)
    assert entry.is_file()
    assert (restore_dir / "media" / "a.png").read_bytes() == payload


def test_pack_rejects_unsafe_paths(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "main.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        transport.pack_file_object(run_dir, "main.jsonl", ["../evil.jsonl"])
    with pytest.raises(ValueError):
        transport.pack_file_object(run_dir, "../main.jsonl", ["main.jsonl"])
    with pytest.raises(ValueError):
        transport.pack_file_object(run_dir, "main.jsonl", ["missing.jsonl"])


def test_restore_rejects_bad_object(tmp_path):
    with pytest.raises(ValueError):
        transport.restore_file_object({"format": "other"}, tmp_path)
    with pytest.raises(ValueError):
        transport.restore_file_object({"format": "crossmodal-files-v1",
                                       "files": []}, tmp_path)
    with pytest.raises(ValueError):
        transport.restore_file_object(
            {"format": "crossmodal-files-v1", "entrypoint": "../x.jsonl",
             "files": [{"path": "../x.jsonl", "encoding": "utf-8", "content": ""}]},
            tmp_path)


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"entrypoint": "a.jsonl"}), encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "a.jsonl" and relative == "a.jsonl"

    empty = tmp_path / "only_one"
    empty.mkdir()
    (empty / "c.jsonl").write_text("", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(empty)
    assert entry.name == "c.jsonl"

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "x.jsonl").write_text("", encoding="utf-8")
    (ambiguous / "y.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        transport.resolve_entrypoint(ambiguous)


def test_public_node_strips_temporary_output_file():
    node = {"operator": "image_text_semantic_scoring", "outputFile": "/tmp/x.jsonl",
            "statistics": {"input": 1}}
    visible = public_node(node)
    assert "outputFile" not in visible
    assert node["outputFile"] == "/tmp/x.jsonl"
