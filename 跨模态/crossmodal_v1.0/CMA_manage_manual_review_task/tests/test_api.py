"""HTTP 接口测试（TestClient）：三接口信封、阶段形态、路径安全与任务隔离。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from tests.conftest import (ENTRY_NAME, uncertain_record, with_review)
from utils import dispatch as dp


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    return TestClient(app_module.app), tmp_path / "out"


def rows_for(payload_state: str, count: int = 3):
    rows = []
    for index in range(count):
        sample_id = "api-%s-%02d" % (payload_state, index)
        if payload_state == "pending":
            rows.append(uncertain_record(sample_id))
        elif payload_state == "pass":
            rows.append(with_review(uncertain_record(sample_id), "pass"))
        elif payload_state == "fail":
            rows.append(with_review(uncertain_record(sample_id), "fail",
                                    dimensions={"semantics": "fail"}))
        else:
            raise ValueError(payload_state)
    return rows


def build_zip(rows) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(ENTRY_NAME,
                         "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
    return buffer.getvalue()


def test_form1_zip_returns_chainable_reference(client):
    http, _root = client
    body = http.post('/predict?detection=M25&uniqueId=chain_zip',
        files={'file': ('input.zip', build_zip(rows_for('pending')), 'application/zip')}).json()
    assert body['status'] == 1, body.get('package_error', body)
    assert body['outputFile']['mode'] == 'path-reference'
    assert Path(body['outputFile']['zip']).is_file()


def upload(client, unique_id: str, payload: bytes, filename: str = "bundle.zip"):
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": (filename, payload, "application/zip")})


def test_health_declares_three_interfaces(client):
    http, _root = client
    body = http.get("/health").json()
    assert body["operator"] == dp.OPERATOR_NAME
    assert body["operatorNumber"] == 25
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False


def test_openapi_exposes_only_three_business_routes(client):
    http, _root = client
    routes = set(http.get("/openapi.json").json()["paths"])
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert routes == {"/uploadFile", "/predict", "/removeFile"}


def test_upload_predict_intermediate_and_stage_end(client):
    """[v4.2] 阶段内/阶段末统一返回 outputFile + result，不再返回 lineageFile。"""
    http, _root = client
    response = upload(http, "api_task_1", build_zip(rows_for("pending")))
    assert response.json()["code"] == 0
    assert response.json()["data"]["entrypoint"] == ENTRY_NAME

    middle = http.post("/predict", json={"uniqueId": "api_task_1",
                                         "isLastOperator": False,
                                         "config": {"enabled": True}}).json()
    assert middle["code"] == 0
    assert set(middle["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in middle["data"]
    assert middle["data"]["status"] == "waiting_manual"
    assert middle["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    summary = middle["data"]["result"]
    assert summary["operator"] == dp.OPERATOR_NAME
    assert summary["uniqueId"] == "api_task_1"
    assert summary["statistics"]["tasks_generated"] == 3
    assert summary["statistics"]["conservation_ok"] is True
    assert summary["status"] == "waiting_manual"

    last = http.post("/predict", json={"uniqueId": "api_task_1",
                                       "isLastOperator": True}).json()
    assert last["code"] == 0
    assert set(last["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = last["data"]["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == dp.MAIN_NAME
    names = [item["path"] for item in file_object["files"]]
    assert dp.MAIN_NAME in names and dp.TASKS_NAME in names and dp.REPORT_NAME in names
    assert dp.MAIN_NAME in [item["path"] for item in middle["data"]["outputFile"]["files"]]


def test_stage_end_file_object_can_be_restored_and_chained(client, tmp_path):
    http, _root = client
    upload(http, "api_chain", build_zip(rows_for("pending")))
    last = http.post("/predict", json={"uniqueId": "api_chain",
                                       "isLastOperator": True,
                                       "config": {"enabled": True}}).json()
    from utils import transport

    restored = transport.restore_file_object(last["data"]["outputFile"],
                                            tmp_path / "restored")
    assert restored.name == dp.MAIN_NAME


def test_two_unique_ids_are_isolated(client):
    http, root = client
    upload(http, "iso_a", build_zip(rows_for("pending", 2)))
    upload(http, "iso_b", build_zip(rows_for("pass", 3)))
    first = http.post("/predict", json={"uniqueId": "iso_a", "isLastOperator": True,
                                        "config": {"enabled": True}}).json()
    second = http.post("/predict", json={"uniqueId": "iso_b", "isLastOperator": True,
                                         "config": {"enabled": True}}).json()
    assert first["data"]["result"]["statistics"]["tasks_generated"] == 2
    assert second["data"]["result"]["statistics"]["tasks_generated"] == 3
    assert second["data"]["result"]["statistics"]["returned_to_pool"] == 3
    assert first["data"]["result"]["uniqueId"] == "iso_a"
    assert (root / "iso_a").is_dir() and (root / "iso_b").is_dir()


def test_disabled_operator_reports_disabled_status(client):
    http, _root = client
    upload(http, "disabled_task", build_zip(rows_for("pending")))
    body = http.post("/predict", json={"uniqueId": "disabled_task",
                                       "isLastOperator": False}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["status"] == "disabled"
    assert body["data"]["result"]["statistics"]["tasks_generated"] == 0


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
    upload(http, "cross_a", build_zip(rows_for("pending")))
    upload(http, "cross_b", build_zip(rows_for("pending")))
    foreign = root / "cross_b" / "input" / ENTRY_NAME
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
    upload(http, "bool_task", build_zip(rows_for("pending")))
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


def test_frozen_config_cannot_be_overridden(client):
    http, _root = client
    upload(http, "frozen_task", build_zip(rows_for("pending")))
    for config in ({"max_review_count": 5}, {"eligible": ["uncertain"]},
                   {"trigger": "auto"}, {"enabled": "true"}):
        body = http.post("/predict", json={"uniqueId": "frozen_task",
                                           "isLastOperator": False,
                                           "config": config}).json()
        assert body["code"] == 1, config


def test_unknown_config_key_is_rejected(client):
    http, _root = client
    upload(http, "unknown_cfg", build_zip(rows_for("pending")))
    body = http.post("/predict", json={"uniqueId": "unknown_cfg", "isLastOperator": False,
                                       "config": {"nope": 1}}).json()
    assert body["code"] == 1


def test_enabled_override_takes_effect(client):
    http, _root = client
    upload(http, "enabled_task", build_zip(rows_for("fail")))
    body = http.post("/predict", json={"uniqueId": "enabled_task", "isLastOperator": False,
                                       "config": {"enabled": True,
                                                  "manual_platform_available": True}}).json()
    assert body["code"] == 0
    stats = body["data"]["result"]["statistics"]
    assert stats["tasks_generated"] == 3
    assert stats["dispatched"] == 3
    assert stats["concluded_rejected"] == 3


def test_remove_file_is_idempotent_and_isolated(client):
    http, root = client
    upload(http, "rm_a", build_zip(rows_for("pending")))
    upload(http, "rm_b", build_zip(rows_for("pending")))
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


def test_non_zip_single_jsonl_upload_gets_manifest(client):
    http, _root = client
    payload = ("\n".join(json.dumps(item, ensure_ascii=False)
                         for item in rows_for("pending")) + "\n").encode("utf-8")
    response = upload(http, "single_file", payload, filename=ENTRY_NAME)
    assert response.json()["code"] == 0
    assert response.json()["data"]["entrypoint"] == ENTRY_NAME


# --------------------------------------------------------------------------- #
# [v4.2] 形态一：单文件流（multipart/form-data）
# --------------------------------------------------------------------------- #

def _post_form1(http, rows, *, unique_id: str = "api_form1",
                detection: str = "M25", data=None):
    payload = ("\n".join(json.dumps(item, ensure_ascii=False) for item in rows)
               + "\n").encode("utf-8")
    return http.post("/predict", params={"detection": detection,
                                         "uniqueId": unique_id},
                     files={"file": ("review_candidates.jsonl", payload,
                                     "application/json")},
                     data=data or {})


def test_single_file_stream_is_noop_success_without_tasks(client):
    """无复核任务时形态一也必须空操作成功（``status=1``，绝不伪造任务）。"""
    http, _root = client
    body = _post_form1(http, rows_for("pending")).json()
    assert body["status"] == 1
    assert body["review_tasks"] == []
    assert body["review_results"] == []
    assert body["process_status"] == "disabled"


def test_single_file_stream_dispatches_tasks_with_override(client):
    http, _root = client
    body = _post_form1(http, rows_for("pending"),
                       data={"enabled": "true"}).json()
    assert body["status"] == 1
    assert len(body["review_tasks"]) == 3
    first = body["review_tasks"][0]
    assert first["review_task_id"] and first["sample_id"]
    assert first["task_status"] == "waiting_manual"
    assert first["trigger"] == "uncertain"
    # 待人工样本同样写入结论台账（decision=pending_manual），但绝不伪造人工 verdict
    assert {item["decision"] for item in body["review_results"]} == {"pending_manual"}
    assert all(item["verdict"] is None for item in body["review_results"])


def test_single_file_stream_writes_conclusions(client):
    http, _root = client
    body = _post_form1(http, rows_for("pass"),
                       data={"enabled": "true",
                             "manual_platform_available": "true"}).json()
    assert body["status"] == 1
    assert [item["verdict"] for item in body["review_results"]] == ["pass"] * 3
    assert body["review_tasks"][0]["task_status"] == "dispatched"


def test_single_file_stream_rejects_mismatch_and_missing_file(client):
    http, _root = client
    mismatch = _post_form1(http, rows_for("pending"), detection="M26").json()
    assert mismatch["status"] == 0 and "detection" in mismatch["msg"]
    missing = http.post("/predict", params={"detection": "M25"},
                        files={"other": ("x.txt", b"x", "text/plain")}).json()
    assert missing["status"] == 0 and "file" in missing["msg"]


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    http, _root = client
    response = http.post("/predict", params={"detection": "m25", "operatorId": "m26"},
                         json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
