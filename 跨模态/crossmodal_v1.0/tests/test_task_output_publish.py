# -*- coding: utf-8 -*-
"""mty_compliance 任务级输出挂载点测试（taskId/输出根解析 + 调用点行为）。"""
from __future__ import annotations

# [v4.2] 血缘/任务级血缘输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import importlib.util
import json
import sys
from pathlib import Path

# 按文件路径、以唯一模块名加载 canonical（避免与 test_lineage_contract 的
# `utils.mty_compliance` 在 sys.modules 里串包——两个包同名模块只能活一个）。
_MODULE_PATH = Path(__file__).parents[1] / "CMA_package_aligned_corpus" / "utils" / "mty_compliance.py"
_SPEC = importlib.util.spec_from_file_location("mty_compliance_canonical", _MODULE_PATH)
mty = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mty
_SPEC.loader.exec_module(mty)  # noqa: E402


# --------------------------------------------------------------------------- #
# _task_output_context：taskId 与输出根的解析优先级
# --------------------------------------------------------------------------- #

def test_context_prefers_manifest_task_id_and_env_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_OUTPUT_ROOT", str(tmp_path / "explicit"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output_dir"))
    context = mty._task_output_context(tmp_path, {"taskId": "task001"}, {})
    assert context == ("task001", tmp_path / "explicit")


def test_context_falls_back_to_output_dir_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output_dir"))
    context = mty._task_output_context(tmp_path, {"taskId": "task001"}, {})
    assert context == ("task001", tmp_path / "output_dir")


def test_context_uses_task_dir_ancestor_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    run_dir = tmp_path / "task001_23" / "23_operator" / "run001"
    run_dir.mkdir(parents=True)
    context = mty._task_output_context(run_dir, {"taskId": "task001"}, {})
    assert context == ("task001", tmp_path)


def test_context_none_without_task_id(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    assert mty._task_output_context(tmp_path, {}, {}) is None
    assert mty._task_output_context(tmp_path, {"taskId": "  "}, {}) is None


# --------------------------------------------------------------------------- #
# 挂载点：阶段末算子 prepare_stage_result 触发任务级输出
# --------------------------------------------------------------------------- #

def _lineage_rows(uid: str, task_id: str = "task001") -> list:
    file_id = uid.split("_", 1)[1]
    return [
        {"type": "file", "writtenBy": "engineering", "formatVersion": "1.0",
         "uniqueId": uid, "taskId": task_id, "fileId": file_id, "file": "a.jpg",
         "bytes": 10, "checksum": "abc", "sourceRef": "ledger://x",
         "datasetVersion": "v1", "ownerTeam": "跨模态语料-算子组", "rowIndex": 0,
         "startedAt": "2026-10-19T10:00:00+08:00"},
        {"type": "stage_segment", "writtenBy": "operator", "stage": "S6",
         "name": "数据集交付",
         "operators": [{"seq": 1, "operator": "delivery_package",
                        "operatorNumber": 23, "operatorVersion": "v1",
                        "stageId": "S6", "stageName": "数据集交付", "runId": "run001",
                        "request": {"uniqueId": uid},
                        "process": {"action": "PROCESS", "detail": "ok",
                                    "rowsIn": 1, "rowsOut": 1},
                        "output": "/workspace/op.out",
                        "ts": "2026-10-19T10:01:00+08:00", "costMs": 1}]},
        {"type": "outcome", "writtenBy": "engineering", "uniqueId": uid,
         "verdict": "PASS", "delivered": True},
    ]


def test_prepare_stage_result_publishes_task_output(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    run_dir = tmp_path / "task001_23" / "23_operator" / "run001"
    run_dir.mkdir(parents=True)
    ids = ["task001_file01"]
    records = [{"operator": "delivery_package", "operator_version": "v1",
                "uniqueId": uid, "status": "success", "stageId": "S6",
                "runId": "run001", "inputFile": "input/%s.jsonl" % uid,
                "startedAt": "2026-10-13T00:00:00+08:00"} for uid in ids]
    result = {"status": "success", "uniqueIds": ids,
              "lineage": records[-1], "stageLineage": records,
              "runDir": str(run_dir), "taskId": "task001",
              "outputFile": str(run_dir / "out.jsonl")}
    (run_dir / "out.jsonl").write_text("{}\n", encoding="utf-8")
    old = mty.IS_STAGE_LAST_OPERATOR
    mty.IS_STAGE_LAST_OPERATOR = True
    try:
        mty.prepare_stage_result(result, request_is_last=True, run_dir=run_dir)
    finally:
        mty.IS_STAGE_LAST_OPERATOR = old
    output_dir = tmp_path / "task001" / "output" / "lineage" / "jsonl"
    files = sorted(output_dir.glob("lineage_*.jsonl"))
    assert files, "阶段末算子应把本阶段血缘写入任务级输出"
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "file"
    # 规范行序：outcome 收尾；交付算子（M23）交付成功时最后是 deliver_supplement
    assert rows[-1]["type"] in ("outcome", "deliver_supplement")
    assert any(r["type"] == "outcome" and r.get("verdict") == "PASS" for r in rows)


def test_prepare_stage_result_skips_publish_for_non_stage_last(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("TASK_OUTPUT_ENABLED", "1")
    run_dir = tmp_path / "task001_24" / "24_operator" / "run001"
    run_dir.mkdir(parents=True)
    ids = ["task001_file01"]
    records = [{"operator": "spot_check_writeback", "operator_version": "v1",
                "uniqueId": uid, "status": "success", "stageId": "S7",
                "runId": "run001", "inputFile": "input/%s.jsonl" % uid,
                "startedAt": "2026-10-13T00:00:00+08:00"} for uid in ids]
    result = {"status": "success", "uniqueIds": ids,
              "lineage": records[-1], "stageLineage": records,
              "runDir": str(run_dir), "taskId": "task001",
              "outputFile": str(run_dir / "out.jsonl")}
    (run_dir / "out.jsonl").write_text("{}\n", encoding="utf-8")
    old = mty.IS_STAGE_LAST_OPERATOR
    mty.IS_STAGE_LAST_OPERATOR = False
    try:
        mty.prepare_stage_result(result, request_is_last=False, run_dir=run_dir)
    finally:
        mty.IS_STAGE_LAST_OPERATOR = old
    assert not (tmp_path / "task001" / "output").exists()
