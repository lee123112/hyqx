"""共享工具测试：路径安全、流式读写、任务锁、血缘拆分与文件对象传输。"""
from __future__ import annotations

import json

import pytest

from utils import io as op_io
from utils import lineage as op_lineage
from utils import transport
from utils.logger import Logger, format_line
from utils.schemas import validate_predict_payload


# ------------------------------------------------------------------ uniqueId / 路径

@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "a/b", "a\\b", "C:", "con", "NUL",
                                 "a\0b", None, 123])
def test_validate_unique_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        op_io.validate_unique_id(bad)


def test_validate_unique_id_accepts_task_key():
    assert op_io.validate_unique_id(" task_01-file ") == "task_01-file"


def test_task_dir_is_isolated_per_unique_id(tmp_path):
    first = op_io.task_dir(tmp_path, "task_a")
    second = op_io.task_dir(tmp_path, "task_b")
    assert first != second
    assert first.parent == second.parent == tmp_path.resolve()
    assert first.is_dir() and second.is_dir()


def test_resolve_in_task_blocks_traversal(tmp_path):
    task = op_io.task_dir(tmp_path, "task_a")
    inside = task / "input" / "a.jsonl"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}\n", encoding="utf-8")
    assert op_io.resolve_in_task(task, "input/a.jsonl") == inside.resolve()
    assert op_io.resolve_in_task(task, str(inside)) == inside.resolve()
    for bad in ("../task_b/input/a.jsonl", "..", "", "   "):
        with pytest.raises(ValueError):
            op_io.resolve_in_task(task, bad)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        op_io.resolve_in_task(task, str(outside))


def test_safe_media_path_rules():
    assert op_io.safe_media_path("media/cat.png") == "media/cat.png"
    assert op_io.safe_media_path("./media/cat.png") == "media/cat.png"
    assert op_io.safe_media_path("media\\cat.png") == "media/cat.png"
    for bad in ("", "  ", "/abs/cat.png", "../cat.png", "http://a/cat.png",
                "\\\\host\\share\\cat.png"):
        with pytest.raises(ValueError):
            op_io.safe_media_path(bad)


def test_copy_media_reports_carried_and_missing(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "cat.png").write_bytes(b"png-bytes")
    run_dir = tmp_path / "run"
    carried, missing = op_io.copy_media(run_dir, tmp_path, ["media/cat.png",
                                                            "media/none.png"])
    assert carried == ["media/cat.png"]
    assert missing == ["media/none.png"]
    assert (run_dir / "media" / "cat.png").read_bytes() == b"png-bytes"


# ------------------------------------------------------------------ JSONL

def test_read_jsonl_reports_every_problem(tmp_path):
    path = tmp_path / "in.jsonl"
    path.write_bytes(
        b'{"a": 1}\n'          # 1 正常
        b'\n'                   # 2 空行
        b'not json\n'           # 3 语法错误
        b'[1, 2]\n'             # 4 非对象
        b'\xff\xfe{"a": 1}\n'   # 5 编码错误
    )
    rows = list(op_io.read_jsonl(path))
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0][1] == {"a": 1} and rows[0][2] is None
    assert rows[1][2] == op_io.BLANK_LINE
    assert rows[2][2] == "json_parse_error"
    assert rows[3][2] == "record_not_object"
    assert rows[4][2] == "encoding_error"


def test_atomic_write_replaces_and_counts(tmp_path):
    target = tmp_path / "out.jsonl"
    count = op_io.atomic_write_jsonl(target, [{"a": 1}, {"a": 2}])
    assert count == 2
    assert target.read_text(encoding="utf-8").count("\n") == 2
    assert not (tmp_path / "out.jsonl.tmp").exists()
    op_io.atomic_write_jsonl(target, [{"a": 3}])
    assert len(target.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_atomic_write_refuses_same_path_as_source(tmp_path):
    target = tmp_path / "same.jsonl"
    target.write_text('{"keep": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        op_io.atomic_write_jsonl(target, [{"a": 1}], source_path=target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": 1}


def test_next_run_id_increments(tmp_path):
    assert op_io.next_run_id(tmp_path) == "run001"
    (tmp_path / "run001").mkdir()
    (tmp_path / "run003").mkdir()
    (tmp_path / "other").mkdir()
    assert op_io.next_run_id(tmp_path) == "run004"


def test_task_lock_is_exclusive_and_released(tmp_path):
    with op_io.task_lock(tmp_path, "task_a"):
        with pytest.raises(op_io.TaskBusy):
            with op_io.task_lock(tmp_path, "task_a", timeout=0):
                pass
    with op_io.task_lock(tmp_path, "task_a", timeout=0):
        pass
    assert (tmp_path / ".locks").is_dir()


# ------------------------------------------------------------------ 血缘
# [v4.2] 血缘输出已停用：以下血缘构建/追加/拆分/公开化用例整体 skip（代码保留存档）

@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_lineage_build_append_load_roundtrip(tmp_path):
    node = op_lineage.build_lineage("op16", "v1", "task_a", "S3", "in.jsonl",
                                    "out.jsonl", "success", "t0", "t1", {"k": 1},
                                    {"input": 1}, run_id="run001",
                                    formal_operator_number=16)
    for key in ("operator", "operator_version", "uniqueId", "inputFile", "status",
                "startedAt", "finishedAt", "config", "statistics", "outputFile"):
        assert key in node
    assert node["formalOperatorNumber"] == 16
    op_lineage.append_lineage(tmp_path, node)
    op_lineage.append_lineage(tmp_path, dict(node, runId="run002"))
    history = op_lineage.load_lineage_history(tmp_path)
    assert len(history) == 2
    stage, prior = op_lineage.partition_lineage(history, "S3", "task_a")
    assert len(stage) == 2 and prior == []
    other, prior = op_lineage.partition_lineage(history, "S3", "task_b")
    assert other == [] and len(prior) == 2


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_partition_lineage_dedupes_identical_nodes():
    node = op_lineage.build_lineage("op16", "v1", "t", "S3", "i", "o", "success",
                                    "t0", "t1", {}, {})
    stage, _prior = op_lineage.partition_lineage([node, dict(node)], "S3", "t")
    assert len(stage) == 1


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_public_node_drops_temporary_output_only():
    node = op_lineage.build_lineage("op16", "v1", "t", "S3", "i", "tmp/out.jsonl",
                                    "success", "t0", "t1", {}, {})
    visible = op_lineage.public_node(node)
    assert "outputFile" not in visible
    assert visible["operator"] == "op16" and node["outputFile"] == "tmp/out.jsonl"


def test_snapshot_writes_utf8_json(tmp_path):
    path = op_lineage.snapshot(tmp_path, "x.json", {"中文": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"中文": 1}


# ------------------------------------------------------------------ 文件对象传输

def test_pack_and_restore_file_object(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "media").mkdir(parents=True)
    (run_dir / "16_whole_alignment.jsonl").write_text('{"a": "中文"}\n', encoding="utf-8")
    (run_dir / "16_review.jsonl").write_text("", encoding="utf-8")
    (run_dir / "media" / "cat.png").write_bytes(b"\x89PNG-bytes")
    files = transport.pack_file_object(run_dir, "16_whole_alignment.jsonl", [
        "16_whole_alignment.jsonl", "16_review.jsonl", "media/cat.png"])
    assert files["format"] == transport.FORMAT
    assert files["entrypoint"] == "16_whole_alignment.jsonl"
    encodings = {item["path"]: item["encoding"] for item in files["files"]}
    assert encodings["16_whole_alignment.jsonl"] == "utf-8"
    assert encodings["media/cat.png"] == "base64"

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(files, restored)
    assert entry.name == "16_whole_alignment.jsonl"
    assert entry.read_text(encoding="utf-8") == '{"a": "中文"}\n'
    assert (restored / "media" / "cat.png").read_bytes() == b"\x89PNG-bytes"


def test_pack_rejects_unsafe_or_missing_paths(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        transport.pack_file_object(tmp_path, "missing.jsonl", ["a.jsonl"])
    with pytest.raises(ValueError):
        transport.pack_file_object(tmp_path, "a.jsonl", ["../escape.jsonl"])
    with pytest.raises(ValueError):
        transport.pack_file_object(tmp_path, "a.jsonl", ["a.jsonl", "a.jsonl"])


def test_restore_rejects_bad_payload(tmp_path):
    with pytest.raises(ValueError):
        transport.restore_file_object({"format": "other"}, tmp_path)
    with pytest.raises(ValueError):
        transport.restore_file_object(
            {"format": transport.FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "../escape", "content": "x"}]}, tmp_path)
    with pytest.raises(ValueError):
        transport.restore_file_object(
            {"format": transport.FORMAT, "entrypoint": "a.jsonl",
             "files": [{"path": "b.jsonl", "encoding": "hex", "content": "00"}]}, tmp_path)


def test_resolve_entrypoint_manifest_and_fallback(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"entrypoint": "demo.jsonl"}),
                                            encoding="utf-8")
    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "demo.jsonl" and relative == "demo.jsonl"

    lonely = tmp_path / "lonely"
    (lonely / "sub").mkdir(parents=True)
    (lonely / "sub" / "only.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(lonely)
    assert relative == "sub/only.jsonl"

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (ambiguous / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        transport.resolve_entrypoint(ambiguous)

    bad = tmp_path / "badmanifest"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"entrypoint": "nope.jsonl"}),
                                       encoding="utf-8")
    with pytest.raises(ValueError):
        transport.resolve_entrypoint(bad)


# ------------------------------------------------------------------ 请求校验

def test_validate_predict_payload_accepts_contract_shape():
    request = validate_predict_payload({"uniqueId": " t1_f1 ", "isLastOperator": False,
                                        "inputFile": "input/a.jsonl",
                                        "config": {"auto_align_threshold": 0.3}})
    assert request["uniqueId"] == "t1_f1"
    assert request["isLastOperator"] is False
    assert request["config"] == {"auto_align_threshold": 0.3}


@pytest.mark.parametrize("payload", [
    None, [], "text",
    {"isLastOperator": False},
    {"uniqueId": "t1_f1"},
    {"uniqueId": "t1_f1", "isLastOperator": 1},
    {"uniqueId": "t1_f1", "isLastOperator": "false"},
    {"uniqueId": "t1_f1", "isLastOperator": False, "operator": "other"},
    {"uniqueId": "t1_f1", "isLastOperator": False, "config": {"weights": "/x"}},
    {"uniqueId": "t1_f1", "isLastOperator": False, "config": []},
    {"uniqueId": "t1_f1", "isLastOperator": False, "inputFile": 5},
])
def test_validate_predict_payload_rejects(payload):
    with pytest.raises(ValueError):
        validate_predict_payload(payload)


# ------------------------------------------------------------------ 日志

def test_logger_writes_action_and_time(tmp_path):
    path = tmp_path / "run.log"
    logger = Logger(path)
    logger("start", "uniqueId=t1")
    content = path.read_text(encoding="utf-8")
    assert "[start]" in content and "uniqueId=t1" in content
    assert format_line("a", "b").startswith("[")
