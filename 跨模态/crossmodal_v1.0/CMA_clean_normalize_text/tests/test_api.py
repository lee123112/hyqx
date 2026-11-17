"""三接口测试：/predict（JSON 与 multipart）、/downloadFile、/removeFile 与安全边界。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from utils import transport

from conftest import make_zip

UID = "demoS2_f1"
ENTRY = "demo.jsonl"


def _post(client, uid=UID, last=False, config=None, input_file=None):
    body = {"uniqueId": uid, "isLastOperator": last}
    body["config"] = {"enabled":True, **(config or {})}
    if input_file is not None:
        body["inputFile"] = input_file
    return client.post("/predict", json=body)


def test_predict_json_config_only_returns_unified_response(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = _post(client, last=False)
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["success"] is True
    data = payload["data"]
    # [v4.2] 形态二响应统一：始终含 outputFile + result，不再返回血缘
    assert set(data) == {"operator", "status", "outputFile", "result"}
    assert data["operator"] == "clean_normalize_text"
    assert data["status"] == "success"
    assert data["result"]["process_status"] == "partial"
    assert data["result"]["statistics"]["output"] == 7
    assert data["outputFile"]["format"] == "crossmodal-files-v1"
    assert data["outputFile"]["entrypoint"] == "03_text_cleaned.jsonl"


def test_index_restrict_no_download_endpoint(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = client.get("/downloadFile", params={"uniqueId": UID})
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"]["error"] == "not_implemented"


def test_predict_multipart_upload_and_replace(client, output_root, sample_dir, tmp_path):
    archive = make_zip(sample_dir, tmp_path / "bundle.zip")
    request_json = json.dumps({"uniqueId": "upload_case", "isLastOperator": False})
    with open(archive, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("bundle.zip", stream, "application/zip")})
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    # 重复上传同一 uniqueId 幂等：覆盖 input 后仍成功
    with open(archive, "rb") as stream:
        again = client.post("/predict", data={"request": request_json},
                            files={"file": ("bundle.zip", stream, "application/zip")})
    assert again.status_code == 200
    # [v4.2] 旧 multipart（带 request 字段）仍走形态二统一响应
    assert again.json()["data"]["result"]["process_status"] == "partial"
    assert (output_root / "upload_case" / "input" / "demo.jsonl").is_file()


def test_predict_is_last_operator_returns_real_output_content(client, output_root, stage,
                                                              tmp_path):
    stage(output_root, UID, ENTRY)
    response = _post(client, last=True)
    assert response.status_code == 200
    data = response.json()["data"]
    # [v4.2] 形态二响应统一：data 为 operator/status/outputFile/result，无血缘
    assert set(data) == {"operator", "status", "outputFile", "result"}
    assert data["outputFile"]["entrypoint"] == "03_text_cleaned.jsonl"
    paths = {item["path"] for item in data["outputFile"]["files"]}
    assert {"03_text_cleaned.jsonl", "03_rejects.jsonl", "manifest.json",
            "media/img_001.png"} <= paths
    # 必须能恢复到全新空目录并解析
    restored = tmp_path / "restored"
    entry = transport.restore_file_object(data["outputFile"], restored)
    assert entry.is_file()
    lines = [json.loads(line) for line in entry.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 7
    for record in lines:
        for field in ("image", "video", "audio"):
            value = record.get(field)
            if isinstance(value, str):
                assert (restored / value).is_file(), value


def test_predict_missing_is_last_operator_is_422(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = client.post("/predict", json={"uniqueId": UID})
    assert response.status_code == 422
    assert response.json()["success"] is False


def test_predict_string_is_last_operator_is_422(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = client.post("/predict", json={"uniqueId": UID, "isLastOperator": "false"})
    assert response.status_code == 422


def test_predict_unknown_field_is_422(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = client.post("/predict", json={"uniqueId": UID, "isLastOperator": False,
                                             "downloadUrl": "http://x/y"})
    assert response.status_code == 422


def test_predict_invalid_unique_id_is_400(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    for bad in ("..", "a/b", "a\\b", "C:", "CON", ""):
        response = _post(client, uid=bad)
        assert response.status_code == 400, bad
        assert response.json()["data"]["error"] == "invalid_unique_id", bad


def test_predict_input_file_escape_is_400(client, output_root, stage, tmp_path):
    stage(output_root, "escape_case", ENTRY)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"sample_id": "x", "text": "y"}\n', encoding="utf-8")
    for bad in ("../outside.jsonl", str(outside), "..\\outside.jsonl"):
        response = _post(client, uid="escape_case", input_file=bad)
        assert response.status_code == 400, bad
        assert response.json()["data"]["error"] == "invalid_input_file", bad


def test_predict_missing_input_is_400(client, output_root):
    response = _post(client, uid="no_input_case")
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "input_missing"


def test_predict_business_override_applied(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = _post(client, config={"enabled": True,
                                     "operator": {"name": "hijacked"},
                                     "server": {"max_files": 1}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["operator"] == "clean_normalize_text"  # 部署身份不被请求改写
    assert data["result"]["statistics"]["tokenizer"] == "not_performed"


def test_predict_invalid_config_regex_is_failure_envelope(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    response = _post(client, config={"ad_patterns": ["([未闭合"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["code"] == 1
    assert payload["data"]["status"] == "failed"
    # [v4.2] 血缘输出已停用：失败响应不再携带血缘字段
    assert "lineageFile" not in payload["data"]


def test_upload_zip_traversal_rejected(client, output_root, sample_dir, tmp_path):
    archive = make_zip(sample_dir, tmp_path / "evil.zip",
                       extra_entries=[("../evil.jsonl", b"{}\n", 0o600 << 16)])
    request_json = json.dumps({"uniqueId": "evil_case", "isLastOperator": False})
    with open(archive, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("evil.zip", stream, "application/zip")})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_archive"
    assert not (output_root / "evil.jsonl").exists()


def test_upload_zip_absolute_and_symlink_rejected(client, output_root, sample_dir, tmp_path):
    absolute = make_zip(sample_dir, tmp_path / "abs.zip",
                        extra_entries=[("/abs.jsonl", b"{}\n", 0o600 << 16)])
    request_json = json.dumps({"uniqueId": "abs_case", "isLastOperator": False})
    with open(absolute, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("abs.zip", stream, "application/zip")})
    assert response.status_code == 400

    link = make_zip(sample_dir, tmp_path / "link.zip",
                    extra_entries=[("media/link.png", b"x", 0o120777 << 16)])
    request_json = json.dumps({"uniqueId": "link_case", "isLastOperator": False})
    with open(link, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("link.zip", stream, "application/zip")})
    assert response.status_code == 400


def test_upload_zip_duplicate_member_rejected(client, output_root, sample_dir, tmp_path):
    archive = make_zip(sample_dir, tmp_path / "dup.zip",
                       extra_entries=[("demo.jsonl", b"{}\n", 0o600 << 16)])
    request_json = json.dumps({"uniqueId": "dup_case", "isLastOperator": False})
    with open(archive, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("dup.zip", stream, "application/zip")})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_archive"


def test_upload_zip_exceeds_file_limit_rejected(client, output_root, sample_dir, tmp_path,
                                                monkeypatch):
    import yaml

    import app as app_module

    config = yaml.safe_load(Path(app_module.CONFIG_PATH).read_text(encoding="utf-8"))
    config["server"]["max_files"] = 2
    small = tmp_path / "small_limit.yaml"
    small.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_PATH", str(small))

    archive = make_zip(sample_dir, tmp_path / "many.zip")
    request_json = json.dumps({"uniqueId": "limit_case", "isLastOperator": False})
    with open(archive, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("many.zip", stream, "application/zip")})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_archive"


def test_upload_zip_without_entry_rejected(client, output_root, tmp_path):
    archive = tmp_path / "no_entry.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "no jsonl here")
    request_json = json.dumps({"uniqueId": "no_entry_case", "isLastOperator": False})
    with open(archive, "rb") as stream:
        response = client.post("/predict", data={"request": request_json},
                               files={"file": ("no_entry.zip", stream, "application/zip")})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_archive"


def test_predict_unsupported_content_type(client):
    response = client.post("/predict", content=b"plain", headers={"content-type": "text/plain"})
    assert response.status_code == 415


def test_predict_malformed_json_body(client):
    response = client.post("/predict", content=b"{not json",
                           headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_remove_file_then_repeat(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    assert _post(client, last=False).status_code == 200
    assert (output_root / UID).is_dir()
    first = client.post("/removeFile", json={"uniqueId": UID})
    assert first.status_code == 200
    assert first.json()["data"] == {"uniqueId": UID, "deleted": True}
    assert not (output_root / UID).exists()
    assert not (output_root / ".locks" / ("%s.lock" % UID)).exists()
    again = client.post("/removeFile", json={"uniqueId": UID})
    assert again.status_code == 200
    assert again.json()["success"] is False
    assert again.json()["data"]["deleted"] is False


def test_remove_file_invalid_unique_id(client, output_root):
    response = client.post("/removeFile", json={"uniqueId": "../x"})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# [v4.2] 形态一（单文件流）：无 request 字段的 multipart + detection 别名
# --------------------------------------------------------------------------- #

def test_single_file_flow_text_upload(client, output_root):
    """形态一：上传单个 txt，合成单记录 JSONL 清洗，返回处理型 status。"""
    uid = "sf_text_case"
    resp = client.post("/predict?detection=M04&uniqueId=%s" % uid,
                       files={"file": ("caption.txt", "ab\x00cd".encode("utf-8"),
                                       "text/plain")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 1, body
    assert body["encoding"] == "UTF-8"
    assert body["text"] == "03_text_cleaned.jsonl"
    # 合成的单记录 JSONL 已清洗（表1 口径：控制字符移除）
    cleaned = next((output_root / uid).rglob("03_text_cleaned.jsonl"))
    row = json.loads(cleaned.read_text(encoding="utf-8").splitlines()[0])
    assert row["sample_id"] == "caption"
    assert row["text"]["content"] == "abcd"
    # [v4.2] 输出记录 status 双字段
    assert row["processing"]["03"]["status"] == 1
    assert row["processing"]["03"]["process_status"] == "success"


def test_single_file_flow_detection_alias_mismatch(client, output_root):
    resp = client.post("/predict?detection=M99&uniqueId=sf_bad_alias",
                       files={"file": ("a.txt", b"hello", "text/plain")})
    assert resp.json()["status"] == 0
    conflict = client.post("/predict?detection=M04&operatorId=M05&uniqueId=sf_bad_alias",
                           files={"file": ("a.txt", b"hello", "text/plain")})
    assert conflict.json()["status"] == 0


def test_single_file_flow_media_rejected(client, output_root):
    resp = client.post("/predict?uniqueId=sf_media_case",
                       files={"file": ("a.png", b"\x89PNG\r\n\x1a\nxx", "image/png")})
    assert resp.json()["status"] == 0
    assert "JSON" in resp.json()["msg"]


def test_single_file_flow_jsonl_passthrough(client, output_root):
    uid = "sf_jsonl_case"
    record = {"sample_id": "s1", "text": "A　B<b>x</b>"}
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    resp = client.post("/predict?operatorId=clean_normalize_text&uniqueId=%s" % uid,
                       files={"file": ("one.jsonl", payload, "application/json")})
    assert resp.json()["status"] == 1, resp.json()


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["operator"] == "text_normalization"
