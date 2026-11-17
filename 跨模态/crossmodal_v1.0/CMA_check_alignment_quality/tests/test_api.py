"""三接口测试：统一信封、两种 isLastOperator 形态、路径安全与任务隔离。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as api
from app import app
from tests.conftest import png_bytes, write_mp4, write_wav

client = TestClient(app)


@pytest.fixture(autouse=True)
def _output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "api_out"
    monkeypatch.setenv("OUTPUT_DIR", str(target))
    return target


def _bundle(media_root: Path) -> bytes:
    """构造一个含入口 JSONL、manifest 与真实媒体的 ZIP。"""
    media_root.mkdir(parents=True, exist_ok=True)
    write_wav(media_root / "tone.wav")
    write_mp4(media_root / "clip_a.mp4")
    rows = [
        {"sample_id": "api-0001",
         "text": {"content": "金毛犬在草地上接飞盘"},
         "image": {"path": "media/ok_a.png", "brisque": 30.0},
         "qc": {"reverse_verify": {"passed": True, "score": 0.6,
                                   "check_status": "checked"}}},
        {"sample_id": "api-0002", "text": {"content": "单模态样本"}},
        {"sample_id": "api-0003", "video": "media/clip_a.mp4",
         "audio": {"path": "media/tone.wav", "snr_db": 19.0}},
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("20_candidate_pool.jsonl",
                         "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
        archive.writestr("media/ok_a.png", png_bytes())
        archive.writestr("media/tone.wav", (media_root / "tone.wav").read_bytes())
        archive.writestr("media/clip_a.mp4", (media_root / "clip_a.mp4").read_bytes())
        archive.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": "20_candidate_pool.jsonl",
            "artifacts": [{"path": "20_candidate_pool.jsonl", "role": "main"}],
        }))
    return buffer.getvalue()


def _upload(unique_id: str, payload: bytes, name: str = "bundle.zip") -> dict:
    response = client.post("/uploadFile?uniqueId=%s" % unique_id,
                           files={"file": (name, payload, "application/zip")})
    return response.json()


def test_upload_predict_and_remove_happy_path(tmp_path: Path) -> None:
    media = tmp_path / "media_src"
    assert _upload("task_api_a", _bundle(media))["code"] == 0

    middle = client.post("/predict", json={"uniqueId": "task_api_a",
                                           "isLastOperator": False}).json()
    assert middle["code"] == 0
    assert set(middle["data"]) == {"operator", "status", "outputFile", "result"}
    assert middle["data"]["operator"] == "alignment_qc"
    assert middle["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert middle["data"]["result"]["statistics"]["input"] > 0

    last = client.post("/predict", json={"uniqueId": "task_api_a",
                                        "isLastOperator": True}).json()
    assert last["code"] == 0
    assert set(last["data"]) == {"operator", "status", "outputFile", "result"}
    assert last["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert "lineageFile" not in last["data"]

    removed = client.post("/removeFile?uniqueId=task_api_a").json()
    assert removed["code"] == 0
    assert client.post("/removeFile?uniqueId=task_api_a").json()["code"] == 0  # 幂等


def test_predict_without_upload_fails(tmp_path: Path) -> None:
    body = client.post("/predict", json={"uniqueId": "never_uploaded",
                                         "isLastOperator": False}).json()
    assert body["code"] == 1
    assert body["data"] == {}


def test_download_file_is_not_implemented() -> None:
    response = client.get("/downloadFile?uniqueId=x")
    assert response.status_code == 404
    assert response.json()["code"] == 1


def test_openapi_exposes_only_three_business_routes() -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert paths == {"/uploadFile", "/predict", "/removeFile"}


def test_health_endpoint() -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["operator"] == "alignment_qc"
    assert body["operatorNumber"] == 20


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "C:", "aux"])
def test_illegal_unique_id_rejected_on_upload(bad: str) -> None:
    response = client.post("/uploadFile?uniqueId=%s" % bad,
                           files={"file": ("a.jsonl", b"{}\n", "application/json")})
    assert response.json()["code"] == 1


def test_illegal_unique_id_rejected_on_remove() -> None:
    assert client.post("/removeFile?uniqueId=..%2Fescape").json()["code"] == 1


def test_non_boolean_is_last_operator_is_rejected(tmp_path: Path) -> None:
    _upload("task_api_b", _bundle(tmp_path / "m"))
    response = client.post("/predict", json={"uniqueId": "task_api_b",
                                             "isLastOperator": "false"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    _upload("task_api_c", _bundle(tmp_path / "m"))
    response = client.post("/predict", json={
        "uniqueId": "task_api_c", "isLastOperator": False,
        "config": {"not_a_real_key": 1}})
    assert response.json()["code"] == 1


def test_deployment_identity_cannot_be_overridden(tmp_path: Path) -> None:
    _upload("task_api_d", _bundle(tmp_path / "m"))
    response = client.post("/predict", json={
        "uniqueId": "task_api_d", "isLastOperator": False,
        "config": {"stage_id": "S9"}})
    assert response.json()["code"] == 1


def test_cross_task_input_file_is_rejected(tmp_path: Path) -> None:
    _upload("task_api_e", _bundle(tmp_path / "m"))
    other_root = Path(__import__("os").environ["OUTPUT_DIR"])
    foreign = other_root / "task_api_f" / "input" / "20_candidate_pool.jsonl"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("{}\n", encoding="utf-8")

    response = client.post("/predict", json={
        "uniqueId": "task_api_e", "isLastOperator": False, "inputFile": str(foreign)})
    assert response.json()["code"] == 1


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    response = client.post("/uploadFile?uniqueId=task_slip",
                           files={"file": ("b.zip", buffer.getvalue(), "application/zip")})
    assert response.json()["code"] == 1
    assert not (Path(__import__("os").environ["OUTPUT_DIR"]).parent / "escape.jsonl").exists()


def test_two_unique_ids_are_isolated(tmp_path: Path) -> None:
    _upload("task_iso_a", _bundle(tmp_path / "ma"))
    _upload("task_iso_b", _bundle(tmp_path / "mb"))
    first = client.post("/predict", json={"uniqueId": "task_iso_a",
                                          "isLastOperator": True}).json()
    second = client.post("/predict", json={"uniqueId": "task_iso_b",
                                           "isLastOperator": True}).json()
    assert first["code"] == 0 and second["code"] == 0
    assert first["data"]["result"]["uniqueId"] == "task_iso_a"
    assert second["data"]["result"]["uniqueId"] == "task_iso_b"

    # 删除一个任务不影响另一个
    client.post("/removeFile?uniqueId=task_iso_a")
    again = client.post("/predict", json={"uniqueId": "task_iso_b",
                                          "isLastOperator": False}).json()
    assert again["code"] == 0


def test_upload_single_jsonl(tmp_path: Path) -> None:
    payload = json.dumps({"sample_id": "s1", "text": {"content": "单独上传"}},
                         ensure_ascii=False).encode("utf-8") + b"\n"
    body = _upload("task_single", payload, name="20_candidate_pool.jsonl")
    assert body["code"] == 0
    assert body["data"]["fileCount"] == 1
    result = client.post("/predict", json={"uniqueId": "task_single",
                                           "isLastOperator": False}).json()
    assert result["code"] == 0


def test_remove_does_not_touch_other_tasks(tmp_path: Path) -> None:
    _upload("task_keep", _bundle(tmp_path / "m"))
    _upload("task_drop", _bundle(tmp_path / "m"))
    client.post("/removeFile?uniqueId=task_drop")
    root = Path(__import__("os").environ["OUTPUT_DIR"])
    assert (root / "task_keep").is_dir()
    assert not (root / "task_drop").is_dir()


def test_form2_detection_operator_id_conflict_rejected():
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m20", "operatorId": "m21"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]


def _write_manifest_bundle(manifest: dict, files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


def test_form1_locator_honors_manifest_with_empty_main_and_ledgers(tmp_path: Path) -> None:
    (tmp_path / "19_candidate_pool.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "19_pending.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "format": "crossmodal-manifest-v1",
        "entrypoint": "19_candidate_pool.jsonl",
    }), encoding="utf-8")

    assert api._locate_form1_entry(tmp_path) == tmp_path / "19_candidate_pool.jsonl"


def test_form1_http_zip_accepts_empty_manifest_entrypoint_with_multiple_ledgers() -> None:
    payload = _write_manifest_bundle({
        "format": "crossmodal-manifest-v1",
        "entrypoint": "19_candidate_pool.jsonl",
        "artifacts": [
            {"path": "19_candidate_pool.jsonl", "role": "main"},
            {"path": "19_pending.jsonl", "role": "sidecar"},
            {"path": "19_rejects.jsonl", "role": "sidecar"},
        ],
    }, {
        "19_candidate_pool.jsonl": "",
        "19_pending.jsonl": "{}\n",
        "19_rejects.jsonl": "{}\n",
    })
    response = client.post("/predict?uniqueId=task_empty_manifest",
                           files={"file": ("bundle.zip", payload, "application/zip")})

    body = response.json()
    assert body["status"] == 1
    assert Path(body["outputFile"]["zip"]).is_file()
    assert body["result"]["statistics"]["empty_input"] is True


@pytest.mark.parametrize("manifest", [
    "{bad json",
    json.dumps({"format": "wrong", "entrypoint": "main.jsonl"}),
    json.dumps({"format": "crossmodal-manifest-v1"}),
    json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": "missing.jsonl"}),
    json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": "C:/outside.jsonl"}),
    json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": "../outside.jsonl"}),
    json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": "nested\\main.jsonl"}),
    json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": "nested"}),
])
def test_form1_locator_rejects_invalid_manifest_even_with_unique_jsonl(
        tmp_path: Path, manifest: str) -> None:
    (tmp_path / "main.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(manifest, encoding="utf-8")
    (tmp_path / "nested").mkdir()

    with pytest.raises(ValueError, match="无法识别输入入口"):
        api._locate_form1_entry(tmp_path)


@pytest.mark.parametrize("filename", ["main.jsonl", "main.json"])
def test_form1_locator_keeps_unique_legacy_file_compatibility(
        tmp_path: Path, filename: str) -> None:
    target = tmp_path / filename
    target.write_text("" if filename.endswith("jsonl") else "{}", encoding="utf-8")
    assert api._locate_form1_entry(tmp_path) == target.resolve()


def test_form1_locator_resolves_nested_manifest_entrypoint(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "main.jsonl"
    nested.parent.mkdir()
    nested.write_text("", encoding="utf-8")
    (tmp_path / "side.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "format": "crossmodal-manifest-v1",
        "entrypoint": "nested/main.jsonl",
    }), encoding="utf-8")
    assert api._locate_form1_entry(tmp_path) == nested.resolve()
