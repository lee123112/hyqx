"""三个正式接口与阶段响应形态测试（FastAPI TestClient）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import fixtures
from utils import audio_text
from utils import io as io_utils
from utils import transport


def bundle_zip(records=None, media=None) -> bytes:
    records = records if records is not None else [
        fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好"),
        fixtures.make_pair("pair-2", transcript="我们一起去公园散步",
                           description="我们一起去公园散步", audio_path="media/seg_02.wav"),
    ]
    media = media if media is not None else fixtures.default_media()
    members = {"demo.jsonl": bytes(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8")}
    members.update(media)
    return fixtures.zip_bytes(members)


def upload(client, unique_id: str, payload: bytes, filename: str = "bundle.zip"):
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": (filename, payload, "application/zip")})


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def test_upload_accepts_bundle(client):
    response = upload(client, "task_up", bundle_zip())
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert "media/seg_01.wav" in body["data"]["files"]


def test_upload_rejects_invalid_unique_id(client):
    response = upload(client, "bad/id", bundle_zip())
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_zip_slip(client):
    payload = fixtures.zip_bytes({"demo.jsonl": b"{}\n", "../evil.jsonl": b"{}\n"})
    response = upload(client, "task_slip", payload)
    assert response.json()["code"] == 1
    assert not (client.app.state.__dict__.get("evil") or False)


def test_download_file_is_not_registered(client):
    assert client.get("/downloadFile", params={"uniqueId": "task_x"}).status_code == 404
    assert client.post("/downloadFile", params={"uniqueId": "task_x"}).status_code == 404
    body = client.get("/downloadFile").json()
    assert body["code"] == 1 and "data" in body


# --------------------------------------------------------------------------- #
# 预测：中间态与阶段末
# --------------------------------------------------------------------------- #

def test_predict_intermediate_returns_output_file(client):
    upload(client, "task_mid", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_mid", "isLastOperator": False})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    # [v4.2] 形态二统一响应：去 lineageFile，统一 outputFile + result
    assert set(data.keys()) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in data
    assert data["operator"] == audio_text.OPERATOR
    assert data["status"] == "success"
    assert data["outputFile"]["format"] == transport.FILE_OBJECT_FORMAT
    assert data["outputFile"]["entrypoint"] == audio_text.MAIN_NAME_DISABLED
    res = data["result"]
    assert res["statistics"]["input_records"] == 2
    assert res["statistics"]["enabled"] is False
    assert res["files"]["outputFile"].endswith(audio_text.MAIN_NAME_DISABLED)


def test_predict_stage_end_returns_file_object(client, tmp_path):
    upload(client, "task_end", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_end", "isLastOperator": True})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data.keys()) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in data
    file_object = data["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == audio_text.MAIN_NAME_DISABLED
    paths = [item["path"] for item in file_object["files"]]
    for name in (audio_text.MAIN_NAME_DISABLED, audio_text.REJECTS_NAME,
                 audio_text.PENDING_NAME, audio_text.SAMPLE_QUARANTINE_NAME, "manifest.json"):
        assert name in paths
    assert data["result"]["statistics"]["input_records"] == 2
    restored = transport.restore_file_object(file_object, tmp_path / "restored")
    assert restored.name == audio_text.MAIN_NAME_DISABLED
    assert restored.read_text(encoding="utf-8").count("\n") == 2


def test_predict_uses_previous_output_as_input(client, output_root):
    upload(client, "task_chain", bundle_zip())
    first = client.post("/predict", json={"uniqueId": "task_chain", "isLastOperator": False}).json()
    # [v4.2] 下一算子 inputFile 取 result.files 中的任务相对路径（不再有 lineageFile）
    previous = first["data"]["result"]["files"]["outputFile"]
    second = client.post("/predict", json={"uniqueId": "task_chain", "isLastOperator": False,
                                           "inputFile": previous})
    assert second.status_code == 200
    assert second.json()["code"] == 0
    task = io_utils.task_dir(output_root, "task_chain")
    assert (task / previous).is_file()


def test_predict_enabled_route_is_rule_only_without_models(client, tmp_path):
    """[v4.2 §4.13] 开启路线 = 上游转写文本 CER 规则；无模型依赖、无降级语义。"""
    upload(client, "task_degr", bundle_zip())
    # [v4.2] 不再有模型/权重配置键：开启路线即按 CER 规则打分
    config = {"enabled": True}
    response = client.post("/predict", json={"uniqueId": "task_degr", "isLastOperator": True,
                                             "config": config})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "success"
    stats = body["data"]["result"]["statistics"]
    assert stats["models_available"] is False
    assert stats["degraded"] is False
    assert stats["scored_records"] == 2
    assert stats["passed_records"] == 2
    entry = [item for item in body["data"]["outputFile"]["files"]
             if item["path"] == audio_text.MAIN_NAME_ENABLED]
    assert entry, "启用路线必须写出音文评分文件"
    rows = [json.loads(line) for line in entry[0]["content"].splitlines() if line.strip()]
    assert len(rows) == 2
    for row in rows:
        node = row["processing"]["13"]
        assert node["status"] == 1 and node["process_status"] == "passed"


# --------------------------------------------------------------------------- #
# 请求校验
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_is_last_operator_must_be_strict_bool(client, value):
    upload(client, "task_bool", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_bool", "isLastOperator": value})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_missing_is_last_operator_rejected(client):
    upload(client, "task_bool2", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_bool2"})
    assert response.status_code == 422


def test_unknown_request_field_rejected(client):
    upload(client, "task_unknown", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_unknown", "isLastOperator": False,
                                             "extraField": 1})
    assert response.status_code == 422


@pytest.mark.parametrize("unique_id", ["", "..", "a/b", "a\\b", "con"])
def test_predict_rejects_invalid_unique_id(client, unique_id):
    response = client.post("/predict", json={"uniqueId": unique_id, "isLastOperator": False})
    assert response.status_code == 422


def test_predict_without_uploaded_input_fails(client):
    response = client.post("/predict", json={"uniqueId": "task_noinput", "isLastOperator": False})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_input_file_outside_task(client, output_root, tmp_path):
    upload(client, "task_scope", bundle_zip())
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    response = client.post("/predict", json={"uniqueId": "task_scope", "isLastOperator": False,
                                             "inputFile": str(outside)})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_bad_config_values(client):
    upload(client, "task_badcfg", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_badcfg", "isLastOperator": False,
                                             "config": {"cosine_threshold": 0.99}})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 1
    assert "ConfigError" in body["msg"]


def test_predict_rejects_readonly_config_key(client):
    upload(client, "task_ro", bundle_zip())
    response = client.post("/predict", json={"uniqueId": "task_ro", "isLastOperator": False,
                                             "config": {"text_model": "other-model"}})
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# 删除与隔离
# --------------------------------------------------------------------------- #

def test_remove_file_is_idempotent(client, output_root):
    upload(client, "task_rm", bundle_zip())
    task = io_utils.task_dir(output_root, "task_rm")
    assert task.exists()
    first = client.post("/removeFile", params={"uniqueId": "task_rm"})
    assert first.status_code == 200
    assert first.json()["code"] == 0
    assert not task.exists()
    second = client.post("/removeFile", params={"uniqueId": "task_rm"})
    assert second.status_code == 200
    assert second.json()["code"] == 0


def test_remove_file_does_not_affect_other_tasks(client, output_root):
    upload(client, "task_keep", bundle_zip())
    upload(client, "task_drop", bundle_zip())
    client.post("/removeFile", params={"uniqueId": "task_drop"})
    assert (Path(output_root) / "task_keep").is_dir()
    assert not (Path(output_root) / "task_drop").exists()


def test_two_unique_ids_do_not_contaminate(client):
    upload(client, "task_a", bundle_zip([fixtures.make_pair(
        "pair-a", transcript="今天天气很好", description="今天天气很好")]))
    upload(client, "task_b", bundle_zip([fixtures.make_pair(
        "pair-b", transcript="我们一起去公园散步", description="我们一起去公园散步",
        audio_path="media/seg_02.wav")]))
    first = client.post("/predict", json={"uniqueId": "task_a", "isLastOperator": True}).json()
    second = client.post("/predict", json={"uniqueId": "task_b", "isLastOperator": True}).json()
    rows_a = _rows(first, audio_text.MAIN_NAME_DISABLED)
    rows_b = _rows(second, audio_text.MAIN_NAME_DISABLED)
    assert rows_a[0]["pair_id"] == "pair-a"
    assert rows_b[0]["pair_id"] == "pair-b"
    # [v4.2] 任务隔离以 result 元数据核对（不再有 lineageFile）
    assert first["data"]["result"]["statistics"]["input_records"] == 1
    assert second["data"]["result"]["statistics"]["input_records"] == 1
    assert first["data"]["result"]["files"]["outputFile"].startswith(audio_text.OPERATOR)
    assert second["data"]["result"]["files"]["outputFile"].startswith(audio_text.OPERATOR)


def _rows(body, name):
    entry = [item for item in body["data"]["outputFile"]["files"] if item["path"] == name]
    assert entry, name
    return [json.loads(line) for line in entry[0]["content"].splitlines() if line.strip()]


def test_detection_alias_registry_operator_id(client):
    """[D2] map.json 登记表英文 operatorId 别名可寻址（走到缺文件分支即证明别名放行）。"""
    response = client.post("/predict", params={"detection": "audio_text_scoring"},
                           files={"other": ("note.txt", b"hi", "text/plain")})
    assert response.json()["msg"] == "缺少上传文件 file"


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "audio_text_scoring"
    assert body["data"]["formalOperatorNumber"] == 13
    assert body["data"]["stageId"] == "S3"
