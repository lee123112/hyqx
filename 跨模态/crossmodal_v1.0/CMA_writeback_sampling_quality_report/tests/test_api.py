"""HTTP 接口测试（TestClient）：三接口信封、阶段形态、路径安全与任务隔离。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from tests.conftest import make_record
from utils import spot_check as sc


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    return TestClient(app_module.app), tmp_path / "out"


def uniform_rows(verdict, count=10):
    return [make_record("api-%02d" % index, verdict=verdict) for index in range(count)]


def test_form1_zip_returns_chainable_reference(client):
    http, _root = client
    body = http.post('/predict?detection=M24&uniqueId=chain_zip',
        files={'file': ('input.zip', build_zip(uniform_rows('pass')), 'application/zip')}).json()
    assert body['status'] == 1, body.get('package_error', body)
    assert body['outputFile']['mode'] == 'path-reference'
    assert Path(body['outputFile']['zip']).is_file()


def build_zip(rows, report: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("24_candidate_pool.jsonl",
                         "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
        if report:
            archive.writestr("20_quality_report.json", json.dumps({
                "format": "crossmodal-quality-report-v1",
                "status": "waiting_writeback",
                "writeback": {"required": True, "writeback_by": "operator_24",
                              "writeback_completed": False,
                              "version_state": "pending_release"},
                "checks": {"text_nonempty": {"ok": len(rows)}},
            }, ensure_ascii=False))
    return buffer.getvalue()


def upload(client, unique_id: str, payload: bytes, filename: str = "bundle.zip"):
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": (filename, payload, "application/zip")})


def test_health_declares_three_interfaces(client):
    http, _root = client
    body = http.get("/health").json()
    assert body["operator"] == sc.OPERATOR_NAME
    assert body["operatorNumber"] == 24
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False


def test_openapi_exposes_only_three_business_routes(client):
    http, _root = client
    routes = set(http.get("/openapi.json").json()["paths"])
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert routes == {"/uploadFile", "/predict", "/removeFile"}


def test_upload_predict_intermediate_and_stage_end(client):
    http, root = client
    response = upload(http, "api_task_1", build_zip(uniform_rows("pass")))
    assert response.json()["code"] == 0
    assert response.json()["data"]["entrypoint"] == "24_candidate_pool.jsonl"

    middle = http.post("/predict", json={"uniqueId": "api_task_1",
                                         "isLastOperator": False}).json()
    assert middle["code"] == 0
    assert set(middle["data"]) == {"operator", "status", "reportStatus",
                                   "outputFile", "result"}
    assert middle["data"]["operator"] == sc.OPERATOR_NAME
    assert middle["data"]["result"]["uniqueId"] == "api_task_1"
    assert middle["data"]["result"]["statistics"]["output"] == 10
    assert middle["data"]["result"]["statistics"]["conservation_ok"] is True

    last = http.post("/predict", json={"uniqueId": "api_task_1",
                                       "isLastOperator": True}).json()
    assert last["code"] == 0
    assert set(last["data"]) == {"operator", "status", "reportStatus",
                                 "outputFile", "result"}
    file_object = last["data"]["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == sc.MAIN_NAME
    names = [item["path"] for item in file_object["files"]]
    assert sc.MAIN_NAME in names
    assert sc.REPORT_NAME in names
    assert "lineageFile" not in last["data"]


def test_stage_end_file_object_can_be_restored_and_chained(client, tmp_path):
    http, _root = client
    upload(http, "api_chain", build_zip(uniform_rows("pass")))
    last = http.post("/predict", json={"uniqueId": "api_chain",
                                       "isLastOperator": True}).json()
    from utils import transport

    restored = transport.restore_file_object(last["data"]["outputFile"],
                                             tmp_path / "restored")
    assert restored.name == sc.MAIN_NAME


def test_two_unique_ids_are_isolated(client):
    http, root = client
    upload(http, "iso_a", build_zip(uniform_rows("pass", 10)))
    upload(http, "iso_b", build_zip(uniform_rows("not_aligned", 6)))
    first = http.post("/predict", json={"uniqueId": "iso_a",
                                        "isLastOperator": True}).json()
    second = http.post("/predict", json={"uniqueId": "iso_b",
                                         "isLastOperator": True}).json()
    assert first["data"]["result"]["statistics"]["output"] == 10
    assert second["data"]["result"]["statistics"]["output"] == 5
    assert first["data"]["result"]["uniqueId"] == "iso_a"
    assert second["data"]["result"]["uniqueId"] == "iso_b"
    assert (root / "iso_a").is_dir() and (root / "iso_b").is_dir()


def test_predict_without_upload_fails_with_envelope(client):
    http, _root = client
    body = http.post("/predict", json={"uniqueId": "never_uploaded",
                                       "isLastOperator": False}).json()
    assert body["code"] == 1
    assert body["data"] == {}


@pytest.mark.parametrize("bad", ["..%2Fescape", "a%2Fb", "CON", ""])
def test_invalid_unique_id_is_rejected(client, bad):
    http, _root = client
    response = http.post("/uploadFile?uniqueId=%s" % bad,
                         files={"file": ("a.jsonl", b"{}\n", "application/json")})
    assert response.status_code == 200
    assert response.json()["code"] == 1


def test_cross_task_input_file_is_rejected(client):
    http, root = client
    upload(http, "cross_a", build_zip(uniform_rows("pass")))
    upload(http, "cross_b", build_zip(uniform_rows("pass")))
    foreign = root / "cross_b" / "input" / "24_candidate_pool.jsonl"
    assert foreign.is_file()
    body = http.post("/predict", json={"uniqueId": "cross_a", "isLastOperator": False,
                                       "inputFile": str(foreign)}).json()
    assert body["code"] == 1
    assert "任务" in body["msg"]


def test_zip_slip_is_rejected_and_not_written(client):
    http, root = client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    body = upload(http, "slip", buffer.getvalue()).json()
    assert body["code"] == 1
    assert not (root / "escape.jsonl").exists()


def test_non_boolean_is_last_operator_is_rejected(client):
    http, _root = client
    upload(http, "bool_task", build_zip(uniform_rows("pass")))
    for value in ("false", 1, None):
        response = http.post("/predict", json={"uniqueId": "bool_task",
                                               "isLastOperator": value})
        assert response.status_code == 422
        assert response.json()["code"] == 1


def test_download_file_is_not_implemented(client):
    http, _root = client
    response = http.get("/downloadFile?uniqueId=x")
    assert response.status_code == 404
    assert response.json()["code"] == 1


def test_config_override_cannot_disable_operator(client):
    http, _root = client
    upload(http, "cfg_task", build_zip(uniform_rows("pass")))
    body = http.post("/predict", json={"uniqueId": "cfg_task", "isLastOperator": False,
                                       "config": {"enabled": False}}).json()
    assert body["code"] == 1
    body = http.post("/predict", json={"uniqueId": "cfg_task", "isLastOperator": False,
                                       "config": {"sample_rate": 0.30}}).json()
    assert body["code"] == 1


def test_sample_rate_override_takes_effect(client):
    http, _root = client
    upload(http, "rate_task", build_zip(uniform_rows("pass", 10)))
    body = http.post("/predict", json={"uniqueId": "rate_task", "isLastOperator": False,
                                       "config": {"sample_rate": 0.20}}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["sampled"] == 2


def test_remove_file_is_idempotent_and_isolated(client):
    http, root = client
    upload(http, "rm_a", build_zip(uniform_rows("pass")))
    upload(http, "rm_b", build_zip(uniform_rows("pass")))
    first = http.post("/removeFile?uniqueId=rm_a").json()
    second = http.post("/removeFile?uniqueId=rm_a").json()
    assert first["code"] == 0 and first["data"]["removed"] is True
    assert second["code"] == 0 and second["data"]["removed"] is False
    assert not (root / "rm_a").exists()
    assert (root / "rm_b").is_dir()


def test_upload_empty_file_is_rejected(client):
    http, _root = client
    body = upload(http, "empty_up", b"").json()
    assert body["code"] == 1


def test_streaming_input_failure_returns_envelope(client):
    http, _root = client
    body = upload(http, "bad_enc", "中文\n".encode("gbk"), filename="cand.jsonl").json()
    assert body["code"] == 0
    result = http.post("/predict", json={"uniqueId": "bad_enc",
                                         "isLastOperator": False}).json()
    assert result["code"] == 1


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    http, _root = client
    response = http.post("/predict", params={"detection": "m24", "operatorId": "m25"},
                         json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
