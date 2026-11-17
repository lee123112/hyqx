"""HTTP 三接口测试（TestClient）：信封、上传、两种 /predict 形态、路径安全与幂等删除。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import inference
from tests.conftest import read_jsonl


@pytest.fixture()
def client(output_root):
    with TestClient(app_module.app) as instance:
        yield instance


def _upload(client, unique_id, bundle_zip):
    data = Path(bundle_zip).read_bytes()
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": ("bundle.zip", data, "application/zip")})


def _predict(client, unique_id, is_last, **extra):
    body = {"uniqueId": unique_id, "isLastOperator": is_last}
    body.update(extra)
    # 旧fixture模拟缺平台；显式授权测试降级，生产默认仍严格。
    body.setdefault("config",{})
    if isinstance(body["config"],dict): body["config"].setdefault("allow_degraded",True)
    return client.post("/predict", json=body)


# ------------------------------------------------------------------ 接口面
def test_openapi_exposes_only_the_three_contract_operations(client):
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/uploadFile", "/predict", "/removeFile"}


def test_download_file_is_not_registered(client):
    response = client.get("/downloadFile?uniqueId=x")
    assert response.status_code == 404
    assert response.json()["code"] == 1


# ------------------------------------------------------------------ 上传
def test_upload_returns_envelope_and_entrypoint(client, bundle_zip):
    response = _upload(client, "task_up", bundle_zip)
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["entrypoint"] == "demo.jsonl"
    assert payload["data"]["files"] >= 5


def test_upload_rejects_zip_slip(client, tmp_path, bundle_zip):
    archive = tmp_path / "slip.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.jsonl", "{}")
    response = client.post("/uploadFile?uniqueId=task_slip",
                           files={"file": ("slip.zip", archive.read_bytes(), "application/zip")})
    assert response.json()["code"] == 1
    assert not (Path(app_module.output_root()).parent / "evil.jsonl").exists()


def test_upload_requires_unique_id(client, bundle_zip):
    response = client.post("/uploadFile",
                           files={"file": ("b.zip", Path(bundle_zip).read_bytes())})
    assert response.status_code == 422
    assert response.json()["code"] == 1


@pytest.mark.parametrize("unique_id", ["../escape", "a/b", "CON", " "])
def test_illegal_unique_id_is_rejected(unique_id, client, bundle_zip):
    response = _upload(client, unique_id, bundle_zip)
    assert response.status_code == 422
    assert response.json()["code"] == 1


# ------------------------------------------------------------------ predict
def test_intermediate_predict_returns_unified_envelope(client, bundle_zip):
    # [v4.2] 响应统一：不再区分阶段末/非阶段末，lineageFile 已去除
    assert _upload(client, "task_mid", bundle_zip).json()["code"] == 0
    response = _predict(client, "task_mid", False)
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert set(payload["data"]) == {"operator", "status", "outputFile", "result"}
    assert payload["data"]["operator"] == "dispose_mismatched_sample"
    assert "lineageFile" not in payload["data"]
    node = payload["data"]["result"]
    assert node["runId"] == "run001"
    assert Path(node["outputFile"]).name == inference.MAIN_NAME
    assert node["statistics"]["input_records"] == 17
    assert node["statistics"]["conservation_ok"] is True


def test_final_predict_returns_file_object_without_lineage(client, bundle_zip):
    assert _upload(client, "task_final", bundle_zip).json()["code"] == 0
    response = _predict(client, "task_final", True)
    data = response.json()["data"]
    assert set(data) == {"operator", "status", "outputFile", "result"}
    assert data["outputFile"]["format"] == "crossmodal-files-v1"
    assert data["outputFile"]["entrypoint"] == inference.MAIN_NAME
    paths = {item["path"] for item in data["outputFile"]["files"]}
    assert inference.LEDGER_NAME in paths
    assert "manifest.json" in paths
    assert data["result"]["statistics"]["released"] == 7


def test_final_predict_can_be_chained_as_stage_input(client, bundle_zip):
    _upload(client, "task_chain", bundle_zip)
    first = _predict(client, "task_chain", False).json()["data"]["result"]
    second = _predict(client, "task_chain", False, inputFile=first["outputFile"])
    assert second.status_code == 200
    node = second.json()["data"]["result"]
    assert node["runId"] == "run002"
    assert node["statistics"]["conservation_ok"] is True


def test_final_predict_chains_previous_run_as_input(client, bundle_zip):
    _upload(client, "task_ctx", bundle_zip)
    _predict(client, "task_ctx", False)
    run_dir = Path(app_module.output_root()) / "task_ctx" / "mismatch_disposal" / "run001"
    entry = run_dir / inference.MAIN_NAME
    response = _predict(client, "task_ctx", True, inputFile=str(entry))
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["result"]["runId"] == "run002"


def test_predict_without_upload_returns_400(client):
    response = _predict(client, "task_noupload", False)
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_is_last_operator_non_boolean(client, bundle_zip):
    _upload(client, "task_bool", bundle_zip)
    for value in ("false", 1, None):
        response = client.post("/predict",
                               json={"uniqueId": "task_bool", "isLastOperator": value})
        assert response.status_code == 422, value
        assert response.json()["code"] == 1


def test_predict_rejects_unknown_field(client, bundle_zip):
    _upload(client, "task_extra", bundle_zip)
    response = client.post("/predict", json={"uniqueId": "task_extra",
                                             "isLastOperator": False, "surprise": 1})
    assert response.status_code == 422


def test_predict_rejects_cross_task_input_file(client, bundle_zip, tmp_path):
    _upload(client, "task_own", bundle_zip)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"sample_id": "x"}\n', encoding="utf-8")
    response = _predict(client, "task_own", False, inputFile=str(outside))
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_task_traversal_input_file(client, bundle_zip):
    _upload(client, "task_trav", bundle_zip)
    response = _predict(client, "task_trav", False,
                        inputFile=str(Path(app_module.output_root()) / "task_trav"
                                      / ".." / ".." / "outside.jsonl"))
    assert response.status_code == 400


def test_predict_config_cannot_override_readonly_or_identity(client, bundle_zip):
    _upload(client, "task_cfg", bundle_zip)
    for overrides in ({"enabled": False}, {"stage_id": "S9"},
                      {"raw_issue_destination": "x"}, {"nope": 1}):
        response = _predict(client, "task_cfg", False, config=overrides)
        assert response.status_code == 200
        assert response.json()["code"] == 1, overrides


def test_predict_config_can_tune_business_parameters(client, bundle_zip):
    _upload(client, "task_cfg2", bundle_zip)
    response = _predict(client, "task_cfg2", False,
                        config={"manual_platform_available": True})
    assert response.json()["code"] == 0
    stats = response.json()["data"]["result"]["statistics"]
    assert stats["waiting_manual"] == 0


# ------------------------------------------------------------------ 任务隔离与清理
def test_two_unique_ids_are_physically_isolated(client, bundle_zip):
    assert _upload(client, "iso_a", bundle_zip).json()["code"] == 0
    assert _upload(client, "iso_b", bundle_zip).json()["code"] == 0
    first = _predict(client, "iso_a", False).json()["data"]["result"]
    second = _predict(client, "iso_b", False).json()["data"]["result"]
    assert first["runId"] == second["runId"] == "run001"
    assert Path(first["outputFile"]).parent.parent.parent.name == "iso_a"
    assert Path(second["outputFile"]).parent.parent.parent.name == "iso_b"


def test_remove_file_is_idempotent_and_scoped(client, bundle_zip):
    _upload(client, "kill_a", bundle_zip)
    _upload(client, "keep_b", bundle_zip)
    _predict(client, "kill_a", False)
    first = client.post("/removeFile?uniqueId=kill_a")
    second = client.post("/removeFile?uniqueId=kill_a")
    assert first.status_code == 200 and first.json()["code"] == 0
    assert second.status_code == 200 and second.json()["code"] == 0
    assert not (Path(app_module.output_root()) / "kill_a").exists()
    assert (Path(app_module.output_root()) / "keep_b").exists()


def test_remove_file_requires_valid_unique_id(client):
    response = client.post("/removeFile?uniqueId=../evil")
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_does_not_reuse_pending_flow_stats_across_tasks(client, bundle_zip):
    _upload(client, "fresh_a", bundle_zip)
    _upload(client, "fresh_b", bundle_zip)
    first = read_jsonl(Path(_predict(client, "fresh_a", False)
                            .json()["data"]["result"]["outputFile"]))
    second = read_jsonl(Path(_predict(client, "fresh_b", False)
                             .json()["data"]["result"]["outputFile"]))
    assert len(first) == len(second) == 7
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["operator"] == "mismatch_disposal"
    assert body["operatorNumber"] == 21
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False
    assert body["model"] is None


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m21", "operatorId": "m22"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
