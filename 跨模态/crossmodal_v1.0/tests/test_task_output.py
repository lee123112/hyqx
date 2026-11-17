# -*- coding: utf-8 -*-
"""task_output.py 的合并/校验/发布规则测试（输出格式.md §3/§4）。"""
from __future__ import annotations

# [v4.2] 血缘/任务级血缘输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import task_output as to  # noqa: E402


# --------------------------------------------------------------------------- #
# 血缘行构造
# --------------------------------------------------------------------------- #

def file_row(uid: str = "t2_a") -> dict:
    return {"type": "file", "writtenBy": "engineering", "formatVersion": "1.0",
            "uniqueId": uid, "taskId": uid.split("_", 1)[0],
            "fileId": uid.split("_", 1)[1] if "_" in uid else uid,
            "file": "a.jpg", "bytes": 10, "checksum": "abc", "sourceRef": "ledger://x",
            "datasetVersion": "v1", "ownerTeam": "跨模态语料-算子组", "rowIndex": 0,
            "startedAt": "2026-10-19T10:00:00+08:00"}


def seg(stage: str, numbers: list, name: str | None = None) -> dict:
    return {"type": "stage_segment", "writtenBy": "operator", "stage": stage,
            "name": name or ("stage" + stage),
            "operators": [{"seq": index + 1, "operator": "op%d" % number,
                           "operatorNumber": number, "operatorVersion": "v1",
                           "stageId": stage, "stageName": "stage" + stage,
                           "runId": "run001", "request": {"uniqueId": "t2_a"},
                           "process": {"action": "PROCESS", "detail": "ok",
                                       "rowsIn": 1, "rowsOut": 1},
                           "output": "/workspace/%s/op%d.out" % (stage, number),
                           "ts": "2026-10-19T10:0%d:00+08:00" % (index % 10),
                           "costMs": 1}
                          for index, number in enumerate(numbers)]}


def oss(stage: str) -> dict:
    return {"type": "oss_supplement", "writtenBy": "engineering", "stage": stage,
            "recordedAfter": "stage zip packaged",
            "oss": {"stageOutput": "local-mirror://a", "zip": "local-mirror://b",
                    "lineage": "local-mirror://c"}}


def outcome(verdict: str, reason: str | None = None) -> dict:
    row = {"type": "outcome", "writtenBy": "engineering", "uniqueId": "t2_a",
           "verdict": verdict, "delivered": verdict == "PASS"}
    if reason:
        row["reason"] = reason
    return row


def deliver() -> dict:
    return {"type": "deliver_supplement", "writtenBy": "engineering",
            "package": "local-mirror://pkg", "versionState": "deliverable"}


def kinds(rows: list) -> list:
    return [row["type"] for row in rows]


def stage_of(rows: list) -> list:
    return [row["stage"] for row in rows if row["type"] == "stage_segment"]


def all_seq(rows: list) -> list:
    return [node["seq"] for row in rows if row["type"] == "stage_segment"
            for node in row["operators"]]


# --------------------------------------------------------------------------- #
# merge_stage_rows：阶段合并
# --------------------------------------------------------------------------- #

def test_merge_into_empty_creates_stage_file_outcome():
    current = [file_row(), seg("S1", [1, 2, 3]), oss("S1"), outcome("PASS")]
    merged = to.merge_stage_rows([], current, "S1")
    assert merged is not None
    assert kinds(merged) == ["file", "stage_segment", "oss_supplement", "outcome"]
    assert to.validate_rows(merged) == []


def test_merge_keeps_prior_stages_and_prior_file_header():
    prior = [file_row(), seg("S1", [1, 2, 3]), oss("S1"), outcome("PASS")]
    current = [file_row(), seg("S2", [4, 5, 8]), oss("S2"), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S2")
    assert stage_of(merged) == ["S1", "S2"]
    assert merged[0]["writtenBy"] == "engineering"          # file 头取已有
    assert all_seq(merged) == [1, 2, 3, 4, 5, 6]            # 跨阶段连续重排
    assert to.validate_rows(merged) == []


def test_merge_replaces_current_stage_on_rerun():
    prior = [file_row(), seg("S1", [1]), oss("S1"), outcome("PASS"),
             seg("S2", [4]), oss("S2")]
    current = [file_row(), seg("S2", [4, 5, 6, 7, 8]), oss("S2"), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S2")
    assert stage_of(merged) == ["S1", "S2"]
    assert [node["operatorNumber"] for node in merged[1]["operators"]] == [1]
    assert [node["operatorNumber"] for node in merged[3]["operators"]] == [4, 5, 6, 7, 8]
    assert to.validate_rows(merged) == []


def test_merge_backfills_missing_stage_from_current():
    prior = [file_row(), seg("S1", [1, 2, 3]), oss("S1"), outcome("PASS")]
    current = [file_row(), seg("S1", [1, 2, 3]), seg("S2", [4, 8]),
               oss("S1"), oss("S2"), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S2")
    assert stage_of(merged) == ["S1", "S2"]
    assert [node["operatorNumber"] for node in merged[1]["operators"]] == [1, 2, 3]
    assert [node["operatorNumber"] for node in merged[3]["operators"]] == [4, 8]


def test_merge_returns_none_without_current_stage():
    prior = [file_row(), seg("S1", [1]), oss("S1"), outcome("PASS")]
    current = [file_row(), seg("S2", [4]), oss("S2"), outcome("PASS")]
    assert to.merge_stage_rows(prior, current, "S3") is None


def test_merge_outcome_deepest_stage_wins():
    prior = [file_row(), seg("S1", [1]), seg("S2", [4]), outcome("PASS")]
    current = [file_row(), seg("S3", [9]), outcome("REJECT", "score_below_isolation")]
    merged = to.merge_stage_rows(prior, current, "S3")
    assert [row for row in merged if row["type"] == "outcome"][0]["verdict"] == "REJECT"


def test_merge_outcome_keeps_prior_when_deeper_stage_present():
    prior = [file_row(), seg("S1", [1]), seg("S2", [4]), seg("S3", [9]),
             outcome("REJECT", "score_below_isolation")]
    current = [file_row(), seg("S2", [4]), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S2")
    assert stage_of(merged) == ["S1", "S2", "S3"]
    assert [row for row in merged if row["type"] == "outcome"][0]["verdict"] == "REJECT"


def test_merge_terminal_verdict_is_sticky():
    prior = [file_row(), seg("S1", [1]), outcome("REJECT", "duplicate_sample_id")]
    current = [file_row(), seg("S2", [4]), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S2")
    assert stage_of(merged) == ["S1", "S2"]
    assert [row for row in merged if row["type"] == "outcome"][0]["verdict"] == "REJECT"


def test_merge_nonblocking_pending_does_not_override_pass():
    prior = [file_row(), seg("S6", [23]), outcome("PASS")]
    current = [file_row(), seg("S8", [26]),
               outcome("PENDING", "route_quota_exhausted")]
    merged = to.merge_stage_rows(prior, current, "S8")
    assert stage_of(merged) == ["S6", "S8"]
    assert [row for row in merged if row["type"] == "outcome"][0]["verdict"] == "PASS"


def test_merge_real_pending_overrides_pass():
    prior = [file_row(), seg("S6", [23]), outcome("PASS")]
    current = [file_row(), seg("S7", [24, 25]),
               outcome("PENDING", "尚未收到资深标注员复核结论，保持待人工、不拦其余样本")]
    merged = to.merge_stage_rows(prior, current, "S7")
    assert [row for row in merged if row["type"] == "outcome"][0]["verdict"] == "PENDING"


def test_merge_deliver_supplement_current_else_prior():
    prior = [file_row(), seg("S6", [23]), outcome("PASS"), deliver()]
    current = [file_row(), seg("S7", [24, 25]), outcome("PASS")]
    merged = to.merge_stage_rows(prior, current, "S7")
    assert kinds(merged)[-1] == "deliver_supplement"

    current2 = [file_row(), seg("S7", [24, 25]), outcome("PASS"),
                {"type": "deliver_supplement", "writtenBy": "engineering",
                 "package": "local-mirror://pkg2", "versionState": "deliverable"}]
    merged2 = to.merge_stage_rows(prior, current2, "S7")
    assert merged2[-1]["package"] == "local-mirror://pkg2"


# --------------------------------------------------------------------------- #
# validate_rows：合同校验
# --------------------------------------------------------------------------- #

def _valid_rows() -> list:
    rows = [file_row(), seg("S1", [1, 2, 3]), oss("S1"), seg("S2", [4, 8]),
            oss("S2"), outcome("PASS"), deliver()]
    to._renumber(rows)
    return rows


def test_validate_accepts_wellformed_rows():
    assert to.validate_rows(_valid_rows()) == []


def test_validate_flags_missing_file_or_outcome():
    rows = _valid_rows()
    errors = to.validate_rows(rows[1:])
    assert any("file" in item for item in errors)
    rows2 = [row for row in _valid_rows() if row["type"] != "outcome"]
    assert any("outcome" in item for item in to.validate_rows(rows2))


def test_validate_flags_broken_seq_and_stage_order():
    rows = _valid_rows()
    rows[1]["operators"][0]["seq"] = 5
    assert any("seq" in item for item in to.validate_rows(rows))
    rows2 = [file_row(), seg("S3", [9]), seg("S1", [1]), oss("S1"), outcome("PASS")]
    assert any("倒序" in item for item in to.validate_rows(rows2))


def test_validate_flags_host_absolute_paths():
    rows = _valid_rows()
    rows[1]["operators"][0]["output"] = r"G:\HYQX\算子\跨模态\a.jsonl"
    assert any("绝对路径" in item for item in to.validate_rows(rows))
    rows2 = _valid_rows()
    rows2[1]["operators"][0]["request"]["callback"] = "https://example.com/download?file=a.jsonl"
    assert to.validate_rows(rows2) == []                    # URL 不得误报


def test_validate_flags_delivered_verdict_mismatch():
    rows = _valid_rows()
    for row in rows:
        if row["type"] == "outcome":
            row["delivered"] = False
    assert any("delivered" in item for item in to.validate_rows(rows))


def test_validate_flags_empty_operator_number():
    rows = _valid_rows()
    rows[1]["operators"][0]["operatorNumber"] = ""
    assert any("operatorNumber" in item for item in to.validate_rows(rows))


# --------------------------------------------------------------------------- #
# publish_stage：任务树集成（临时目录）
# --------------------------------------------------------------------------- #

def _write_jsonl(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _build_stage1(root: Path) -> None:
    """M01(245→本例2条) / M02 / M03 三个任务目录，M01 独有一条被拒记录。"""
    rows_a1 = [file_row("t2_a"), seg("S1", [1]), oss("S1"), outcome("PASS")]
    rows_a2 = [file_row("t2_a"), seg("S1", [2]), outcome("PASS")]
    rows_a3 = [file_row("t2_a"), seg("S1", [3]), oss("S1"), outcome("PASS")]
    rows_b1 = [file_row("t2_b"), seg("S1", [1]), oss("S1"), outcome("REJECT", "duplicate_sample_id")]
    for number, rows in ((1, [rows_a1, rows_b1]), (2, [rows_a2]), (3, [rows_a3])):
        run = root / ("t2_%02d" % number) / ("%02d_operator" % number) / "run001"
        for row in rows:
            uid = row[0]["uniqueId"]
            _write_jsonl(run / "lineage" / ("lineage_%s.jsonl" % uid), row)


def test_publish_stage_aggregates_whole_stage(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    _build_stage1(tmp_path)
    run_dir = tmp_path / "t2_03" / "03_operator" / "run001"
    summary = to.publish_stage(run_dir=run_dir, lineage_files=[], result={},
                               task_id="t2", stage_id="S1", operator_number=3,
                               output_root=tmp_path)
    assert summary["errors"] == []
    assert summary["written"] == 2
    target = tmp_path / "t2" / "output" / "lineage" / "jsonl" / "lineage_t2_a.jsonl"
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert stage_of(rows) == ["S1"]
    assert [node["operatorNumber"] for node in rows[1]["operators"]] == [1, 2, 3]
    assert to.validate_rows(rows) == []
    # M01 独有的被拒记录也要进任务级输出
    target_b = target.parent / "lineage_t2_b.jsonl"
    rows_b = [json.loads(line) for line in target_b.read_text(encoding="utf-8").splitlines()]
    assert [row for row in rows_b if row["type"] == "outcome"][0]["verdict"] == "REJECT"


def test_publish_stage_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    _build_stage1(tmp_path)
    run_dir = tmp_path / "t2_03" / "03_operator" / "run001"
    first = to.publish_stage(run_dir=run_dir, lineage_files=[], result={},
                             task_id="t2", stage_id="S1", operator_number=3,
                             output_root=tmp_path)
    target = tmp_path / "t2" / "output" / "lineage" / "jsonl" / "lineage_t2_a.jsonl"
    before = target.read_text(encoding="utf-8")
    second = to.publish_stage(run_dir=run_dir, lineage_files=[], result={},
                              task_id="t2", stage_id="S1", operator_number=3,
                              output_root=tmp_path)
    assert first["written"] == second["written"] == 2
    assert target.read_text(encoding="utf-8") == before


def test_publish_stage_special_run_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    _build_stage1(tmp_path)
    special = tmp_path / "t2_03_check" / "03_operator" / "run001"
    summary = to.publish_stage(run_dir=special, lineage_files=[], result={},
                               task_id="t2", stage_id="S1", operator_number=3,
                               output_root=tmp_path)
    assert summary["skippedReason"] == "not-main-run"
    assert not (tmp_path / "t2" / "output").exists() or \
        not list((tmp_path / "t2" / "output").rglob("lineage_*.jsonl"))


def test_publish_stage_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "0")
    run_dir = tmp_path / "t2_03" / "op" / "run001"
    summary = to.publish_stage(run_dir=run_dir, lineage_files=[], result={},
                               task_id="t2", stage_id="S1", operator_number=3,
                               output_root=tmp_path)
    assert summary["skippedReason"] == "disabled"


def test_publish_stage_updates_existing_output(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    _build_stage1(tmp_path)
    run3 = tmp_path / "t2_03" / "03_operator" / "run001"
    to.publish_stage(run_dir=run3, lineage_files=[], result={},
                     task_id="t2", stage_id="S1", operator_number=3,
                     output_root=tmp_path)
    # S2：M08 只跑 a（b 被 S1 拒绝不会向后流）
    run8 = tmp_path / "t2_08" / "08_operator" / "run001"
    _write_jsonl(run8 / "lineage" / "lineage_t2_a.jsonl",
                 [file_row("t2_a"), seg("S2", [4, 5, 8]), oss("S2"), outcome("PASS")])
    summary = to.publish_stage(run_dir=run8, lineage_files=[], result={},
                               task_id="t2", stage_id="S2", operator_number=8,
                               output_root=tmp_path)
    assert summary["written"] == 1
    target = tmp_path / "t2" / "output" / "lineage" / "jsonl" / "lineage_t2_a.jsonl"
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert stage_of(rows) == ["S1", "S2"]
    assert all_seq(rows) == [1, 2, 3, 4, 5, 6]
    assert to.validate_rows(rows) == []


def test_publish_stage_copies_and_prunes_package_zip(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    run23 = tmp_path / "t2_23" / "23_operator" / "run001"
    _write_jsonl(run23 / "lineage" / "lineage_t2_a.jsonl",
                 [file_row("t2_a"), seg("S6", [23]), oss("S6"), outcome("PASS"), deliver()])
    old_zip = run23 / "23_pending_package.zip"
    old_zip.write_bytes(b"old")
    to.publish_stage(run_dir=run23, lineage_files=[],
                     result={"packagePath": str(old_zip)},
                     task_id="t2", stage_id="S6", operator_number=23,
                     output_root=tmp_path)
    zip_dir = tmp_path / "t2" / "output" / "data" / "zip"
    assert [p.name for p in zip_dir.glob("*.zip")] == ["23_pending_package.zip"]
    new_zip = run23 / "23_deliverable_package.zip"
    new_zip.write_bytes(b"new")
    to.publish_stage(run_dir=run23, lineage_files=[],
                     result={"packagePath": str(new_zip)},
                     task_id="t2", stage_id="S6", operator_number=23,
                     output_root=tmp_path)
    assert [p.name for p in zip_dir.glob("*.zip")] == ["23_deliverable_package.zip"]


# --------------------------------------------------------------------------- #
# 阶段内节点合并（独立 run 口径不一致时）
# --------------------------------------------------------------------------- #

def test_collect_stage_bundles_prefers_severe_outcome(tmp_path):
    runs = {
        1: tmp_path / "t2_01" / "op" / "run001",
        2: tmp_path / "t2_02" / "op" / "run001",
    }
    _write_jsonl(runs[1] / "lineage" / "lineage_t2_a.jsonl",
                 [file_row("t2_a"), seg("S1", [1]), outcome("PASS")])
    _write_jsonl(runs[2] / "lineage" / "lineage_t2_a.jsonl",
                 [file_row("t2_a"), seg("S1", [2]), outcome("REJECT", "duplicate_sample_id")])
    bundles = to.collect_stage_bundles(runs, "S1")
    rows = bundles["lineage_t2_a.jsonl"]
    assert [node["operatorNumber"] for node in rows[1]["operators"]] == [1, 2]
    assert [row for row in rows if row["type"] == "outcome"][0]["verdict"] == "REJECT"


def test_stage_operators_match_spec():
    assert to.STAGE_OPERATORS == {
        "S1": (1, 2, 3), "S2": (4, 5, 6, 7, 8),
        "S3": (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19),
        "S4": (20, 21), "S5": (22,), "S6": (23,), "S7": (24, 25), "S8": (26,),
    }
    assert to.STAGE_LAST_NUMBERS == (3, 8, 19, 21, 22, 23, 25, 26)
