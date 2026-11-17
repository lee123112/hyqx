"""三接口与公共验收用例测试（DEVELOPMENT_CONTRACT §2、§6）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app as app_module
from tests.conftest import (OUTPUT_ROOT, ROOT, make_zip, predict, read_sample_files,
                            sample_zip_bytes, upload_zip, write_config)
from utils.transport import FILE_OBJECT_FORMAT, restore_file_object


# --------------------------------------------------------------------------- #
# 用例 1：上传 → 中间模式 → 复用中间血缘的 outputFile
# --------------------------------------------------------------------------- #

def test_upload_predict_unified_contract(client, uploaded_fixture, tmp_path):
    response = predict(client, uploaded_fixture, False)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    data = body["data"]
    # [v4.2] 形态二统一响应：operator/status/outputFile/result，无 lineageFile
    assert set(data) == {"operator", "status", "outputFile", "response_mode", "result"}
    assert data["response_mode"] == "inline"
    assert data["operator"] == "extract_video_keyframe"
    assert data["status"] == "success"
    payload = data["outputFile"]
    assert payload["format"] == FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "05_frame_index.jsonl"
    summary = data["result"]
    assert summary["status"] == 1
    assert summary["process_status"] in ("success", "partial")
    # 阶段首必须从 input/ 的上传入口读取（任务相对路径），绝不读取本算子旧运行产物
    assert summary["input"].startswith("input/")
    assert "video_keyframes" not in Path(summary["input"]).parts

    # 阶段链式：outputFile 内容对象恢复后可重新上传，作为阶段内下一轮输入
    restored = tmp_path / "chain"
    entry = restore_file_object(payload, restored)
    assert entry.name == "05_frame_index.jsonl"
    files = {path.relative_to(restored).as_posix(): path.read_bytes()
             for path in restored.rglob("*") if path.is_file()}
    files["manifest.json"] = json.dumps({"format": "crossmodal-manifest-v1",
                                         "entrypoint": "05_frame_index.jsonl"})
    again = upload_zip(client, uploaded_fixture, make_zip(files))
    assert again.json()["code"] == 0, again.text
    second = predict(client, uploaded_fixture, False)
    assert second.json()["code"] == 0, second.text
    assert second.json()["data"]["result"]["input"].endswith("05_frame_index.jsonl")


def test_predict_stage_first_always_locates_upload_entry(client, uploaded_fixture):
    predict(client, uploaded_fixture, False)
    # 再次从 input/ 定位（上传入口优先于任何运行目录中的同名文件）
    again = predict(client, uploaded_fixture, False)
    assert again.json()["code"] == 0, again.text
    assert again.json()["data"]["result"]["input"].startswith("input/")


# --------------------------------------------------------------------------- #
# 用例 2：阶段末文件内容对象可恢复到全新目录并打开全部媒体
# --------------------------------------------------------------------------- #

def test_predict_last_operator_file_object_restorable(client, uploaded_fixture, tmp_path):
    response = predict(client, uploaded_fixture, True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    data = body["data"]
    # [v4.2] isLastOperator 不再改变响应形态：与非阶段末完全一致的统一响应
    assert set(data) == {"operator", "status", "outputFile", "response_mode", "result"}
    assert data["response_mode"] == "inline"
    payload = data["outputFile"]
    assert payload["format"] == FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "05_frame_index.jsonl"
    paths = {item["path"] for item in payload["files"]}
    assert "05_frame_index.jsonl" in paths and "05_rejects.jsonl" in paths
    assert any(path.startswith("media/") for path in paths)
    assert any(path.startswith("05_frames/") for path in paths)
    assert "manifest.json" not in paths  # 清单不塞入 files

    # 恢复到全新临时根，只依赖内容对象
    destination = tmp_path / "fresh"
    entry = restore_file_object(payload, destination)
    import inference as core

    check = core.verify_media_references(entry, destination)
    assert check["ok"], check
    assert check["checked"] > 0


# --------------------------------------------------------------------------- #
# 用例 3：参数与配置失败的统一信封
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload,reason", [
    ({"uniqueId": "ok_id"}, "缺少 isLastOperator"),
    ({"uniqueId": "ok_id", "isLastOperator": "false"}, "字符串布尔"),
    ({"uniqueId": "ok_id", "isLastOperator": 1}, "整数布尔"),
    ({"uniqueId": "ok_id", "isLastOperator": False, "unknownField": 1}, "未知字段"),
    ({"uniqueId": "ok_id", "isLastOperator": False, "config": {"unknown_key": 1}}, "未知 config"),
    ({"uniqueId": "ok_id", "isLastOperator": False, "config": {"stage_id": "S9"}}, "覆盖部署配置"),
    ({"uniqueId": "ok_id", "isLastOperator": False, "config": {"sample_fps": 0}}, "非法取值"),
    ({"uniqueId": "../escape", "isLastOperator": False}, "非法 uniqueId"),
    ({"uniqueId": "", "isLastOperator": False}, "空 uniqueId"),
])
def test_predict_rejects_bad_requests_with_envelope(client, payload, reason):
    response = client.post("/predict", json=payload)
    assert response.status_code == 422, "%s -> %s" % (reason, response.text)
    body = response.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == 1 and body["data"] == {}
    assert body["msg"]


def test_predict_without_uploaded_input_fails_with_envelope(client, unique_id):
    response = predict(client, unique_id, False)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == 1 and body["data"] == {}
    assert "input_not_found" in body["msg"]


def test_predict_rejects_non_json_body(client):
    response = client.post("/predict", content=b"not json",
                           headers={"Content-Type": "application/json"})
    body = response.json()
    assert response.status_code == 422 and body["code"] == 1 and body["data"] == {}


def test_predict_rejects_invalid_input_file_paths(client, uploaded_fixture):
    cases = [
        "../other/x.jsonl",
        str(OUTPUT_ROOT / (uploaded_fixture + "_evil") / "x.jsonl"),
        str(OUTPUT_ROOT / "another_task" / "x.jsonl"),
    ]
    for candidate in cases:
        response = predict(client, uploaded_fixture, False, inputFile=candidate)
        assert response.status_code == 422, candidate
        body = response.json()
        assert body["code"] == 1 and body["data"] == {}


# --------------------------------------------------------------------------- #
# 用例 4：上传安全与删除隔离
# --------------------------------------------------------------------------- #

def test_upload_rejects_zip_slip_and_manifest_problems(client, unique_id):
    slip = upload_zip(client, unique_id, make_zip({"../evil.txt": "x"}))
    assert slip.status_code == 422 and slip.json()["code"] == 1

    missing_entry = upload_zip(client, "%s_b" % unique_id, make_zip({
        "manifest.json": json.dumps({"format": "crossmodal-manifest-v1",
                                     "entrypoint": "nope.jsonl"}),
        "demo.jsonl": "{}\n"}))
    assert missing_entry.status_code == 422 and missing_entry.json()["code"] == 1

    ambiguous = upload_zip(client, "%s_c" % unique_id, make_zip({
        "a.jsonl": "{}\n", "b.jsonl": "{}\n"}))
    assert ambiguous.status_code == 422 and ambiguous.json()["code"] == 1


def test_upload_rejects_case_collision_and_empty_body(client, unique_id):
    collision = upload_zip(client, unique_id, make_zip({
        "demo.jsonl": "{}\n", "Media/A.jpg": "a", "media/a.jpg": "b"}))
    assert collision.status_code == 422 and collision.json()["code"] == 1

    empty = upload_zip(client, "%s_e" % unique_id, b"")
    assert empty.status_code == 422 and empty.json()["code"] == 1


def test_upload_missing_parameters_return_envelope(client):
    no_file = client.post("/uploadFile", params={"uniqueId": "x_f1"})
    assert no_file.status_code == 422 and no_file.json()["code"] == 1

    no_id = client.post("/uploadFile", files={"file": ("demo.jsonl", b"{}\n")})
    assert no_id.status_code == 422 and no_id.json()["code"] == 1
    assert no_id.json()["data"] == {}


def test_upload_enforces_size_and_file_limits(client, unique_id, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CONFIG_PATH",
                        str(write_config(tmp_path / "small", max_unpacked_bytes=64)))

    too_big = upload_zip(client, unique_id, make_zip({"demo.jsonl": "x" * 4096}))
    assert too_big.status_code == 422, too_big.text
    assert too_big.json()["code"] == 1

    monkeypatch.setattr(app_module, "CONFIG_PATH",
                        str(write_config(tmp_path / "few", max_files=2)))
    too_many = upload_zip(client, "%s_m" % unique_id, make_zip({
        "demo.jsonl": "{}\n", "a.txt": "a", "b.txt": "b", "c.txt": "c"}))
    assert too_many.status_code == 422 and too_many.json()["code"] == 1


def test_upload_single_jsonl_without_media(client, unique_id):
    records = {"sample_id": "u1", "text": {"content": "t"}, "video": None}
    other = {"sample_id": "u2", "text": {"content": "t2"}, "video": None}
    body = "\n".join(json.dumps(item, ensure_ascii=False) for item in (records, other)) + "\n"
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("samples.jsonl", body.encode("utf-8"),
                                           "application/jsonl")})
    assert response.json()["code"] == 0, response.text
    assert response.json()["data"]["entrypoint"] == "samples.jsonl"

    result = predict(client, unique_id, False)
    assert result.json()["code"] == 0, result.text
    summary = result.json()["data"]["result"]
    assert Path(summary["input"]).name == "samples.jsonl"
    assert summary["statistics"]["not_applicable"] == 2
    assert summary["statistics"]["conservation_ok"] is True


def test_remove_file_is_isolated_and_wrong_methods_fail(client, unique_id):
    kept = "%s_keep" % unique_id
    doomed = "%s_doomed" % unique_id
    for target in (kept, doomed):
        assert upload_zip(client, target, sample_zip_bytes()).json()["code"] == 0

    removed = client.post("/removeFile", params={"uniqueId": doomed})
    assert removed.status_code == 200
    assert removed.json() == {"code": 0, "msg": "success", "data": {}}
    assert not (OUTPUT_ROOT / doomed).exists()
    assert (OUTPUT_ROOT / kept / "input" / "demo.jsonl").is_file()

    missing = client.post("/removeFile", params={"uniqueId": unique_id})
    assert missing.status_code == 200 and missing.json()["code"] == 0

    no_id = client.post("/removeFile")
    assert no_id.status_code == 422 and no_id.json()["code"] == 1

    wrong = client.delete("/removeFile", params={"uniqueId": kept})
    assert wrong.status_code == 405 and wrong.json()["code"] == 1

    assert client.get("/downloadFile").status_code == 404
    assert client.get("/downloadFile").json()["code"] == 1


def test_framework_generated_errors_use_unified_envelope(client):
    """未知路径 404、方法不符 405 也必须走统一信封，不漏 {"detail": ...}。"""
    for method, path in (("get", "/predict"), ("get", "/removeFile"), ("get", "/uploadFile"),
                         ("put", "/predict"), ("delete", "/predict")):
        response = getattr(client, method)(path)
        assert response.status_code == 405, (method, path, response.text)
        body = response.json()
        assert set(body) == {"code", "msg", "data"}, (method, path, body)
        assert body["code"] == 1 and body["data"] == {}
        assert "method_not_allowed" in body["msg"]

    unknown = client.post("/notExists")
    assert unknown.status_code == 404
    body = unknown.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == 1 and body["data"] == {} and "not_found" in body["msg"]


# --------------------------------------------------------------------------- #
# 用例 5：统计与状态
# --------------------------------------------------------------------------- #

def _zip_with_entry(entry_name: str, records, extra=None):
    files = {entry_name: "\n".join(json.dumps(item, ensure_ascii=False)
                                   for item in records) + "\n"}
    files["manifest.json"] = json.dumps({
        "format": "crossmodal-manifest-v1", "entrypoint": entry_name,
        "artifacts": [{"path": entry_name, "role": "main"}]})
    if extra:
        files.update(extra)
    return make_zip(files)


def _data_of(client, unique_id):
    """[v4.2] 统一响应的 data（operator/status/outputFile/result）。"""
    response = predict(client, unique_id, False)
    assert response.json()["code"] == 0, response.text
    return response.json()["data"]


def test_predict_empty_input_is_explicit(client, unique_id, tmp_path):
    assert upload_zip(client, unique_id,
                      _zip_with_entry("empty.jsonl", [])).json()["code"] == 0
    data = _data_of(client, unique_id)
    stats = data["result"]["statistics"]
    assert stats["empty_input"] is True
    assert stats["output"] == 0 and stats["rejected"] == 0
    assert data["result"]["process_status"] == "success"
    entry = restore_file_object(data["outputFile"], tmp_path / "restored")
    assert entry.read_text(encoding="utf-8") == ""


def test_predict_not_applicable_and_duplicate_and_bad_lines(client, unique_id):
    records = [
        {"sample_id": "d1", "text": {"content": "a"}, "video": None},
        {"sample_id": "d1", "text": {"content": "b"}, "video": None},
        {"sample_id": "d2", "text": {"content": "c"},
         "video": {"path": "media/vid_cfr_2s.mp4"}},
        {"sample_id": "d3", "video": "media/missing.mp4"},
    ]
    lines = [json.dumps(records[0], ensure_ascii=False), "{不是 JSON"]
    lines.extend(json.dumps(item, ensure_ascii=False) for item in records[1:])
    payload = make_zip({
        "demo.jsonl": "\n".join(lines) + "\n",
        "manifest.json": json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
            "artifacts": [{"path": "demo.jsonl", "role": "main"},
                          {"path": "media/vid_cfr_2s.mp4", "role": "media"}]}),
        "media/vid_cfr_2s.mp4": read_sample_files(["media/vid_cfr_2s.mp4"])["media/vid_cfr_2s.mp4"],
    })
    assert upload_zip(client, unique_id, payload).json()["code"] == 0
    data = _data_of(client, unique_id)
    stats = data["result"]["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["input_lines"] == 5
    assert stats["bad_lines"] == 1
    assert stats["duplicates"] == 1
    assert stats["valid_records"] == 4
    assert stats["output"] + stats["rejected"] == 4
    assert stats["not_applicable"] == 1
    assert stats["media_missing"] == 1
    assert stats["rejected"] == 2
    assert data["result"]["process_status"] == "partial"


def test_predict_processing_failure_is_rejected_with_reason(client, unique_id, tmp_path):
    records = [{"sample_id": "brk", "video": "media/broken.mp4"}]
    payload = make_zip({
        "demo.jsonl": json.dumps(records[0], ensure_ascii=False) + "\n",
        "manifest.json": json.dumps({"format": "crossmodal-manifest-v1",
                                     "entrypoint": "demo.jsonl",
                                     "artifacts": [{"path": "demo.jsonl", "role": "main"},
                                                   {"path": "media/broken.mp4",
                                                    "role": "media"}]}),
        "media/broken.mp4": read_sample_files(["media/broken.mp4"])["media/broken.mp4"],
    })
    assert upload_zip(client, unique_id, payload).json()["code"] == 0
    data = _data_of(client, unique_id)
    assert data["result"]["statistics"]["rejected"] == 1
    assert data["result"]["statistics"]["output"] == 0
    restored = tmp_path / "restored"
    restore_file_object(data["outputFile"], restored)
    rejects = [json.loads(line)
               for line in (restored / "05_rejects.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert rejects and rejects[0]["reason"] == "media_unreadable"
    assert rejects[0]["sample_id"] == "brk"


def test_repeat_execution_creates_new_run(client, uploaded_fixture):
    # [v4.2] 响应不再暴露 runId/本地路径：重复执行均成功且输出非空（落盘不覆盖由 test_operator 覆盖）
    first = _data_of(client, uploaded_fixture)
    second = _data_of(client, uploaded_fixture)
    assert first["outputFile"]["files"] and second["outputFile"]["files"]


def test_lock_conflict_returns_task_busy(client, uploaded_fixture):
    lock_path = OUTPUT_ROOT / ".locks" / ("%s.lock" % uploaded_fixture)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999", encoding="ascii")
    try:
        response = predict(client, uploaded_fixture, False)
        assert response.status_code == 400, response.text
        body = response.json()
        # [v4.2] 运行级失败信封 data={"status": "failed"}
        assert body["code"] == 1 and body["data"] == {"status": "failed"}
        assert "task_busy" in body["msg"]
    finally:
        lock_path.unlink()
    assert predict(client, uploaded_fixture, False).json()["code"] == 0


# --------------------------------------------------------------------------- #
# 用例 7：交付件与文档一致性
# --------------------------------------------------------------------------- #

def test_deliverable_files_are_parseable():
    """交付件一致性。

    上传结构对齐甲方《开发规范》后：`map.json`（26 算子统一注册表）与 `Dockerfile`、`inference.py`、
    `weight/` 都收到**算子上层目录**，算子目录只保留 dataset/utils/configs/tests/output/README 等项。
    """
    package_root = ROOT.parent
    registry = json.loads((package_root / "map.json").read_text(encoding="utf-8"))
    entry = next(item for item in registry["operators"].values() if item["package"] == ROOT.name)
    assert entry["operatorId"] == "video_keyframes"
    assert entry["port"] == 8105
    assert entry["entrypoint"]
    # 算子上层目录：甲方规范四件
    for name in ("Dockerfile", "inference.py", "map.json", "requirements.txt"):
        assert (package_root / name).is_file(), name
    assert (package_root / "weight").is_dir()
    # 算子目录：README.md 必填 + 甲方要求的算子说明书
    assert (ROOT / "README.md").is_file()
    assert (ROOT / "OPERATOR_MANUAL.md").is_file()


def test_readme_and_cli_use_same_config():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "configs/config.yaml" in readme
    assert "inference.py" in readme
    assert "8105" in readme


# --------------------------------------------------------------------------- #
# 用例 8：[v4.2] 形态一单文件流协议（multipart/form-data + detection 别名）
# --------------------------------------------------------------------------- #

def test_single_file_form_returns_frames_zip(client, unique_id):
    video = read_sample_files(["media/vid_cfr_2s.mp4"])["media/vid_cfr_2s.mp4"]
    response = client.post("/predict",
                           params={"detection": "extract_video_keyframe",
                                   "uniqueId": unique_id},
                           files={"file": ("vid_cfr_2s.mp4", video, "video/mp4")})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    import io
    import zipfile

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert "05_frame_index.jsonl" in names
    assert any(name.startswith("05_frames/") for name in names)
    index_records = [json.loads(line)
                     for line in archive.read("05_frame_index.jsonl").decode("utf-8").splitlines()
                     if line.strip()]
    # [v4.2] 输出记录 status 双字段：int status + process_status
    processing = index_records[0]["processing"]["05"]
    assert processing["status"] == 1
    assert processing["process_status"] == "success"


def test_single_file_form_accepts_mkv_extension(client, unique_id):
    """[v1.1] 单视频白名单放宽：mkv 扩展名也应进入帧抽取，而不是被扩展名拒绝。"""
    video = read_sample_files(["media/vid_cfr_2s.mp4"])["media/vid_cfr_2s.mp4"]
    response = client.post("/predict",
                           params={"detection": "extract_video_keyframe",
                                   "uniqueId": unique_id},
                           files={"file": ("clip.mkv", video, "video/x-matroska")})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")


def test_single_file_form_detection_alias_and_mismatch(client, unique_id):
    video = read_sample_files(["media/vid_cfr_2s.mp4"])["media/vid_cfr_2s.mp4"]
    ok = client.post("/predict", params={"detection": "M06", "uniqueId": unique_id},
                     files={"file": ("v.mp4", video, "video/mp4")})
    assert ok.status_code == 200, ok.text

    mismatch = client.post("/predict", params={"detection": "M07"},
                           files={"file": ("v.mp4", video, "video/mp4")})
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == 1

    conflict = client.post("/predict",
                           params={"detection": "M06", "operatorId": "extract_video_keyframe"},
                           files={"file": ("v.mp4", video, "video/mp4")})
    assert conflict.status_code == 422
    assert conflict.json()["code"] == 1

    # [D2] map.json 登记表英文 operatorId 别名可寻址
    registry = client.post("/predict", params={"detection": "video_keyframes",
                                               "uniqueId": unique_id},
                           files={"file": ("v.mp4", video, "video/mp4")})
    assert registry.status_code == 200, registry.text


def test_single_file_form_missing_file_and_bad_media(client, unique_id):
    # multipart 表单但缺少 file 字段
    missing = client.post("/predict", params={"detection": "extract_video_keyframe"},
                          files={"other": ("note.txt", b"hi", "text/plain")})
    assert missing.json()["status"] == 0

    broken = client.post("/predict", params={"detection": "06", "uniqueId": unique_id},
                         files={"file": ("broken.mp4",
                                         read_sample_files(["media/broken.mp4"])["media/broken.mp4"],
                                         "video/mp4")})
    assert broken.json()["status"] == 0
    assert broken.json()["msg"]


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["operator"] == "video_keyframes"
