"""MTY 归档（OSS）+ 关系入库 单测。

覆盖规范流程图 F2/F3/F4/F5：拼接血缘与输出文件上传对象存储、阶段产物 zip、
「任务/源文件/血缘文件/阶段/区域(槽)/去向/输出文件」关系入库，以及未配置
对象存储 / 配置错误时的降级行为（**不得抛异常、不得改变业务响应**）。

本文件由打包脚本同步到 26 个算子，内容一致；断言对「阶段末算子」与
「阶段内普通算子」都成立。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils import mty_compliance as mty  # noqa: E402

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

UID = "task001_file01"


def _mirror_path(item) -> Path:
    """把镜像逻辑地址还原成本地文件（验收/排障用）。"""
    return mty.TransportSettings.from_env().mirror_root / item["key"]


def report_roles(result) -> dict:
    return {item["path"]: item.get("role") for item in result["artifacts"]}


@pytest.fixture()
def transport_env(tmp_path, monkeypatch):
    """强制「本地镜像 + 本地 SQLite」，全部写入 tmp_path，不接触生产配置。"""
    monkeypatch.setenv("MTY_TRANSPORT_ENABLED", "1")
    monkeypatch.setenv("MTY_ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("MTY_REGISTRY_BACKEND", "sqlite")
    monkeypatch.setenv("MTY_TRANSPORT_DIR", str(tmp_path / "_mty_transport"))
    for key in ("MTY_REGISTRY_DSN", "MTY_OSS_ENDPOINT", "MTY_OSS_BUCKET",
                "MTY_OSS_ACCESS_KEY_ID", "MTY_OSS_ACCESS_KEY_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _make_run(root: Path):
    run_dir = root / UID / "operator" / "run001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "out.jsonl").write_text('{"sample_id":"s1"}\n', encoding="utf-8")
    nodes = [
        {"operator": "upstream_op", "uniqueId": UID, "runId": "run000", "status": "success",
         "stageId": mty.STAGE_ID, "inputFile": "input/entry.jsonl"},
        {"operator": "current_op", "uniqueId": UID, "runId": "run001", "status": "success",
         "stageId": mty.STAGE_ID, "inputFile": "input/entry.jsonl"},
    ]
    result = {"status": "success", "runDir": str(run_dir), "entrypoint": "out.jsonl",
              "lineage": nodes[1], "stageLineage": nodes, "artifacts": []}
    return run_dir, nodes, result


def _prepare(root: Path, is_last: bool = True):
    run_dir, nodes, result = _make_run(root)
    mty.prepare_stage_result(result, is_last, run_dir=run_dir)
    return run_dir, nodes, result


def _report(run_dir: Path):
    path = run_dir / mty.TRANSPORT_REPORT_NAME
    assert path.is_file(), "缺少 %s" % mty.TRANSPORT_REPORT_NAME
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# uniqueId 强校验
# --------------------------------------------------------------------------- #

def test_unique_id_format_enforced():
    assert mty.validate_unique_id(UID) == UID
    assert mty.validate_unique_id("  %s  " % UID) == UID  # 首尾空白按既有 io 口径剥离
    for bad in ("task001", "t1", "", "a b_c", "_file01", "task001_", "task/001_file01"):
        with pytest.raises(ValueError):
            mty.validate_unique_id(bad)


# --------------------------------------------------------------------------- #
# 血缘补 isLastOperator + 拼接文本 + 阶段产物
# --------------------------------------------------------------------------- #

def test_lineage_carries_is_last_operator(transport_env):
    _run_dir, nodes, _result = _prepare(transport_env)
    for node in nodes:
        assert node["isLastOperator"] is bool(mty.IS_STAGE_LAST_OPERATOR)
        assert node["requestIsLastOperator"] is True
        assert node["stageId"] == mty.STAGE_ID


def test_joined_lineage_is_one_record_per_line(transport_env):
    run_dir, nodes, result = _prepare(transport_env)
    joined = run_dir / mty.STAGE_LINEAGE_NAME
    assert joined.is_file()
    lines = joined.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(nodes)
    for line, node in zip(lines, nodes):
        assert json.loads(line)["operator"] == node["operator"]
    assert mty.STAGE_LINEAGE_NAME in {item["path"] for item in result["artifacts"]}


def test_stage_artifact_only_for_stage_last_operator(transport_env):
    run_dir, _nodes, result = _prepare(transport_env)
    artifact = run_dir / mty.STAGE_ARTIFACT_NAME
    if mty.IS_STAGE_LAST_OPERATOR:
        assert artifact.is_file()
        assert mty.STAGE_ARTIFACT_NAME in {item["path"] for item in result["artifacts"]}
    else:
        assert not artifact.is_file()
    assert not (run_dir / (mty.STAGE_ARTIFACT_NAME + ".partial")).exists()


# --------------------------------------------------------------------------- #
# 归档 + 入库
# --------------------------------------------------------------------------- #

def test_archive_mirrors_lineage_output_and_registers_relation(transport_env):
    run_dir, _nodes, _result = _prepare(transport_env)
    report = _report(run_dir)
    assert report["format"] == "crossmodal-transport-report-v1"
    assert report["uniqueId"] == UID
    assert report["taskId"] == "task001" and report["fileId"] == "file01"

    archived = {item["name"]: item for item in report["archive"]}
    assert mty.STAGE_LINEAGE_NAME in archived and "out.jsonl" in archived
    if mty.IS_STAGE_LAST_OPERATOR:
        assert mty.STAGE_ARTIFACT_NAME in archived
    for item in archived.values():
        assert item["status"] in {"uploaded", "mirrored"}
        if item["status"] == "mirrored":
            assert item["uri"] == "local-mirror://%s" % item["key"]
            assert _mirror_path(item).is_file(), "镜像文件缺失：%s" % item["key"]

    rows = mty.list_relations(UID)
    assert rows, "关系表未写入"
    row = rows[0]
    assert row["task_id"] == "task001" and row["file_id"] == "file01"
    assert row["unique_id"] == UID and row["stage_id"] == mty.STAGE_ID
    assert row["slot_id"] and row["direction"]
    assert row["is_last_operator"] == int(bool(mty.IS_STAGE_LAST_OPERATOR))
    assert str(row["lineage_uri"]).startswith("local-mirror://")
    assert str(row["output_uri"]).startswith("local-mirror://")
    assert row["source_file"] == "input/entry.jsonl"


def test_archive_report_never_leaks_host_paths(transport_env):
    run_dir, _nodes, _result = _prepare(transport_env)
    text = (run_dir / mty.TRANSPORT_REPORT_NAME).read_text(encoding="utf-8")
    assert str(mty.TransportSettings.from_env().mirror_root) not in text
    assert "G:\\\\HYQX" not in text and "local-mirror://" in text


def test_relation_write_is_idempotent(transport_env):
    run_dir, _nodes, result = _prepare(transport_env)
    mty.prepare_stage_result(result, True, run_dir=run_dir)
    mty.prepare_stage_result(result, True, run_dir=run_dir)
    rows = mty.list_relations(UID)
    assert len(rows) == 1
    assert rows[0]["relation_id"] == _report(run_dir)["relationId"]


def test_transport_report_registered_in_manifest(transport_env):
    run_dir, _nodes, result = _prepare(transport_env)
    assert result["artifacts"], "manifest.artifacts 未登记归档报告"
    assert mty.TRANSPORT_REPORT_NAME in {item["path"] for item in result["artifacts"]}
    assert report_roles(result)[mty.TRANSPORT_REPORT_NAME] == mty.TRANSPORT_ARTIFACT_ROLE


def test_unknown_entrypoint_still_reports(transport_env):
    run_dir = transport_env / UID / "operator" / "run900"
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {"status": "success", "runDir": str(run_dir), "artifacts": [],
              "lineage": {"operator": "current_op", "uniqueId": UID, "runId": "run900",
                          "status": "success", "stageId": mty.STAGE_ID}}
    mty.prepare_stage_result(result, True, run_dir=run_dir)
    report = _report(run_dir)
    assert report["archiveStatus"] in {"uploaded", "mirrored", "degraded", "skipped"}
    assert report["registry"]["status"] in {"inserted", "failed", "skipped"}


# --------------------------------------------------------------------------- #
# 降级：配置错误也必须返回业务结果
# --------------------------------------------------------------------------- #

def test_remote_archive_failure_degrades_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("MTY_TRANSPORT_DIR", str(tmp_path / "_mty_transport"))
    monkeypatch.setenv("MTY_ARCHIVE_BACKEND", "oss2")
    monkeypatch.setenv("MTY_OSS_ENDPOINT", "oss-cn-invalid.aliyuncs.com")
    monkeypatch.setenv("MTY_OSS_BUCKET", "invalid-bucket")
    monkeypatch.setenv("MTY_OSS_ACCESS_KEY_ID", "invalid-ak")
    monkeypatch.setenv("MTY_OSS_ACCESS_KEY_SECRET", "invalid-sk")
    entry = mty.archive_object(Path(__file__), "crossmodal/probe/test.json")
    assert entry["status"] in {"failed", "mirrored", "skipped"}
    assert entry["degraded"] is True
    assert entry["error"]
    run_dir, _nodes, _result = _prepare(tmp_path, is_last=True)
    report = _report(run_dir)
    assert report["status"] in {"ok", "degraded", "failed"}


def test_registry_dsn_failure_degrades_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("MTY_TRANSPORT_DIR", str(tmp_path / "_mty_transport"))
    monkeypatch.setenv("MTY_ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("MTY_REGISTRY_BACKEND", "sqlalchemy")
    monkeypatch.setenv("MTY_REGISTRY_DSN", "postgresql+psycopg2://u:p@127.0.0.1:1/none")
    outcome = mty.record_relation({"unique_id": UID, "stage_id": mty.STAGE_ID,
                                   "operator": "current_op", "run_id": "run001"})
    assert outcome["status"] in {"inserted", "failed"}
    if outcome["status"] == "failed":
        assert outcome["error"]
    run_dir, _nodes, _result = _prepare(tmp_path, is_last=True)
    assert (run_dir / mty.TRANSPORT_REPORT_NAME).is_file()


def test_transport_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MTY_TRANSPORT_ENABLED", "0")
    run_dir, _nodes, result = _prepare(tmp_path)
    assert not (run_dir / mty.TRANSPORT_REPORT_NAME).is_file()
    assert result["status"] == "success"


def test_settings_summary_hides_secrets(monkeypatch):
    monkeypatch.setenv("MTY_OSS_ACCESS_KEY_SECRET", "super-secret-value")
    summary = json.dumps(mty.TransportSettings.from_env().summary(), ensure_ascii=False)
    assert "super-secret-value" not in summary


def test_selftest_passes():
    payload = mty.selftest()
    assert payload["ok"] is True, json.dumps(payload, ensure_ascii=False, indent=2)
