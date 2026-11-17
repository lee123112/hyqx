# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "CMA_check_alignment_quality"))
from utils import mty_compliance as mty


def test_per_file_lineage_and_stage_zip(tmp_path):
    ids = [f"task001_file{i:02d}" for i in range(1, 16)]
    records = [
        {"operator": "op-a", "operator_version": "v1", "uniqueId": uid,
         "status": "success", "stageId": "S1", "runId": "run001",
         "inputFile": f"input/{uid}.jsonl", "startedAt": "2026-10-13T00:00:00+08:00"}
        for uid in ids
    ]
    records += [
        {"operator": "op-b", "operator_version": "v1", "uniqueId": uid,
         "status": "success", "stageId": "S2", "runId": "run002",
         "inputFile": f"input/{uid}.jsonl", "startedAt": "2026-10-13T00:01:00+08:00"}
        for uid in ids
    ]
    run_dir = tmp_path / "run001"
    run_dir.mkdir()
    result = {"status": "success", "uniqueIds": ids, "lineage": records[-1], "stageLineage": records,
              "runDir": str(run_dir), "outputFile": str(run_dir / "out.jsonl")}
    (run_dir / "out.jsonl").write_text("{}\n", encoding="utf-8")
    old = mty.IS_STAGE_LAST_OPERATOR
    mty.IS_STAGE_LAST_OPERATOR = True
    try:
        mty.prepare_stage_result(result, request_is_last=True, run_dir=run_dir)
    finally:
        mty.IS_STAGE_LAST_OPERATOR = old
    files = sorted((run_dir / "lineage").glob("lineage_*.jsonl"))
    assert len(files) == 15
    for path in files:
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert lines[0]["type"] == "file"
        assert any(item["type"] == "stage_segment" for item in lines)
        assert any(item["type"] == "outcome" for item in lines)
        assert any(op["isLastOperator"] for item in lines if item["type"] == "stage_segment" for op in item["operators"])
    artifact = run_dir / "stage_artifact.zip"
    assert artifact.is_file()
    with zipfile.ZipFile(artifact) as archive:
        names = set(archive.namelist())
        assert len([n for n in names if n.startswith("lineage/lineage_")]) == 15
        manifest = json.loads(archive.read("stage_artifact_manifest.json"))
        assert manifest["isLastOperator"] is True
        assert len(manifest["lineageFiles"]) == 15


def test_intermediate_does_not_create_stage_zip(tmp_path):
    run_dir = tmp_path / "run001"
    run_dir.mkdir()
    record = {"operator": "op-a", "operator_version": "v1", "uniqueId": "task001_file01",
              "status": "success", "stageId": "S1", "runId": "run001"}
    result = {"status": "success", "lineage": record, "runDir": str(run_dir)}
    old = mty.IS_STAGE_LAST_OPERATOR
    mty.IS_STAGE_LAST_OPERATOR = False
    try:
        mty.prepare_stage_result(result, request_is_last=False, run_dir=run_dir)
    finally:
        mty.IS_STAGE_LAST_OPERATOR = old
    assert not (run_dir / "stage_artifact.zip").exists()
    assert len(list((run_dir / "lineage").glob("lineage_*.jsonl"))) == 1


