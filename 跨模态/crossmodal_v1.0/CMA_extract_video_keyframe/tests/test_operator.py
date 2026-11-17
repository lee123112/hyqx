"""业务算法与 run() 统计守恒测试（TECHNICAL_DESIGN 业务用例表）。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import inference as core
from tests.conftest import CONFIG_PATH, SAMPLE_DIR
from utils import video_processing as vp


# --------------------------------------------------------------------------- #
# 纯函数：帧选择
# --------------------------------------------------------------------------- #

def test_select_frame_indices_cfr_grid():
    # 10fps / 20 帧 / 2.0 秒，按 2 帧每秒采样 -> t=0,0.5,1.0,1.5
    indices = vp.select_frame_indices(frame_count=20, fps=10.0, sample_fps=2.0, max_frames=60)
    assert indices == [0, 5, 10, 15]


def test_select_frame_indices_dedups_when_sampling_above_fps():
    indices = vp.select_frame_indices(frame_count=4, fps=10.0, sample_fps=20.0, max_frames=60)
    assert indices == [0, 1, 2, 3]


def test_select_frame_indices_raises_resource_limit_instead_of_truncating():
    indices=vp.select_frame_indices(500,25.0,1.0,8)
    assert len(indices)==8 and indices[0]==0


@pytest.mark.parametrize("kwargs", [
    {"frame_count": 0, "fps": 10.0, "sample_fps": 1.0, "max_frames": 10},
    {"frame_count": 10, "fps": 0.0, "sample_fps": 1.0, "max_frames": 10},
    {"frame_count": 10, "fps": 10.0, "sample_fps": float("nan"), "max_frames": 10},
    {"frame_count": 10, "fps": 10.0, "sample_fps": 1.0, "max_frames": 0},
    {"frame_count": 10, "fps": 10.0, "sample_fps": 1.0, "max_frames": True},
])
def test_select_frame_indices_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        vp.select_frame_indices(**kwargs)


# --------------------------------------------------------------------------- #
# 纯函数：ffprobe 解析与黑边检测
# --------------------------------------------------------------------------- #

def test_parse_ffprobe_output_detects_cfr_and_vfr():
    cfr = json.dumps({"streams": [{"codec_type": "video", "avg_frame_rate": "25/1",
                                   "r_frame_rate": "25/1", "duration": "2.0",
                                   "nb_frames": "50"}]})
    parsed = vp.parse_ffprobe_output(cfr)
    assert parsed["backend"] == "ffprobe" and parsed["avg_frame_rate"] == 25.0
    assert parsed["vfr_hint"] is False

    vfr = json.dumps({"streams": [{"codec_type": "video", "avg_frame_rate": "24/1",
                                   "r_frame_rate": "30/1", "duration": "2.0"}]})
    assert vp.parse_ffprobe_output(vfr)["vfr_hint"] is True


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"streams": []}',
                                  '{"streams": [{"codec_type": "audio"}]}'])
def test_parse_ffprobe_output_rejects_invalid_payloads(text):
    with pytest.raises(ValueError):
        vp.parse_ffprobe_output(text)


def test_detect_black_borders_conservative_rules():
    base = np.full((40, 40), 120, np.uint8)
    bordered = base.copy()
    bordered[:4, :] = 0
    bordered[-4:, :] = 0
    bordered[:, :4] = 0
    bordered[:, -4:] = 0
    assert vp.detect_black_borders([bordered, bordered]) == (4, 4, 4, 4)

    assert vp.detect_black_borders([base, base]) is None       # 无黑边
    assert vp.detect_black_borders([]) is None                 # 空输入

    too_much = base.copy()
    too_much[:30, :] = 0
    assert vp.detect_black_borders([too_much]) is None          # 超比例：放弃裁剪而非裁成空图


def test_safe_component_strips_unsafe_characters():
    assert vp.safe_component("vk_001") == "vk_001"
    assert vp.safe_component("../../etc/passwd") == "etc_passwd"
    assert vp.safe_component("") == "sample"
    assert vp.safe_component("中文 id") == "id"


# --------------------------------------------------------------------------- #
# 抽帧主流程（真实样例媒体）
# --------------------------------------------------------------------------- #

def _business_config(**overrides):
    config = {"sample_fps": 2.0, "max_frames": 8, "max_duration_seconds": 60.0,
              "crop_black_borders": False, "probe_timeout_seconds": 15.0,
              "unsupported_vfr_policy": "reject", "sample_id": "vk_ut"}
    config.update(overrides)
    return config


def test_extract_frames_on_real_cfr_video(tmp_path):
    source = SAMPLE_DIR / "media" / "vid_cfr_2s.mp4"
    result = vp.extract_frames(source, tmp_path, _business_config())
    assert result["planned_frames"] == 4
    assert result["decoded_frames"] == 4
    assert [frame["timestamp"] for frame in result["frames"]] == [0.0, 0.5, 1.0, 1.5]
    assert [frame["frame_index"] for frame in result["frames"]] == [0, 1, 2, 3]
    assert result["timestamp_source"] == "ffmpeg_input_pts"
    for frame in result["frames"]:
        saved = tmp_path / frame["path"]
        assert saved.is_file() and saved.stat().st_size > 0
        assert frame["width"] > 0 and frame["height"] > 0


def test_extract_frames_crop_black_borders(tmp_path):
    with pytest.raises(ValueError):
        vp.extract_frames(SAMPLE_DIR/'media/vid_border_1s.mp4',tmp_path,_business_config(crop_black_borders=True))


def test_extract_frames_rejects_broken_and_missing_media(tmp_path):
    with pytest.raises(vp.MediaUnreadableError):
        vp.extract_frames(SAMPLE_DIR / "media" / "broken.mp4", tmp_path, _business_config())
    with pytest.raises(vp.MediaUnreadableError):
        vp.extract_frames(SAMPLE_DIR / "media" / "missing.mp4", tmp_path, _business_config())

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(vp.MediaUnreadableError):
        vp.extract_frames(empty, tmp_path, _business_config())


def test_extract_frames_enforces_duration_limit(tmp_path):
    result=vp.extract_frames(SAMPLE_DIR/'media/vid_cfr_2s.mp4',tmp_path,_business_config(max_frames=1))
    assert len(result['frames'])==1
    assert result['duration']>=2.0  # 不按上限裁短或因帧数多拒绝整段。


# --------------------------------------------------------------------------- #
# run()：统计守恒、血缘与拒绝可解释
# --------------------------------------------------------------------------- #

def test_run_conservation_and_media_integrity(tmp_path):
    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_good",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    stats = result["statistics"]
    assert result["status"] == "partial"
    assert stats["conservation_ok"] is True
    assert stats["input"] == 6
    assert stats["output"] + stats["rejected"] == stats["valid_records"] == 6
    assert stats["not_applicable"] == 1
    assert stats["rejected"] == 2
    assert stats["success"] == 3
    assert stats["frames_saved"] == stats["frames_total"] > 0
    assert stats["media_missing"] == 1

    check = core.verify_media_references(result["outputFile"], result["runDir"])
    assert check["ok"], check
    assert check["samples"] == 4

    records = [json.loads(line) for line in
               Path(result["outputFile"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {record["sample_id"]: record for record in records}
    # 不适用记录保留原结构，只标注 05 状态（[v4.2] status 双字段：int status + process_status）
    assert by_id["vk_004"]["video"] is None
    assert by_id["vk_004"]["processing"]["05"]["status"] == 1
    assert by_id["vk_004"]["processing"]["05"]["process_status"] == "not_applicable"
    assert by_id["vk_004"]["video"] is None
    # 成功记录的媒体路径全部重写为本次 run 内相对路径
    assert by_id["vk_001"]["video"]["path"].startswith("media/vk_001/")
    assert by_id["vk_001"]["image"]["path"].startswith("media/vk_001/")
    assert by_id["vk_001"]["processing"]["05"]["status"] == 1
    assert by_id["vk_001"]["processing"]["05"]["process_status"] == "success"
    assert len(by_id["vk_001"]["video"]["frames"]) == 4


def test_run_reports_duplicates_bad_lines_and_reasons(tmp_path):
    result = core.run(str(SAMPLE_DIR / "demo_bad.jsonl"), "vk_ut_bad",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    stats = result["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["input_lines"] == 5
    assert stats["bad_lines"] == 2
    assert stats["duplicates"] == 1
    assert stats["valid_records"] == 3
    assert stats["output"] + stats["rejected"] == 3
    assert result["status"] == "partial"

    rejects = [json.loads(line) for line in
               Path(result["rejectsFile"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sorted(item["reason"] for item in rejects) == [
        "duplicate_sample_id", "invalid_json", "invalid_record", "missing_sample_id"]
    # 坏行带行号，可追溯
    assert all("line" in item for item in rejects)


def test_run_reject_keeps_source_reference_without_fabricating_media(tmp_path):
    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_reject",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    rejects = [json.loads(line) for line in
               Path(result["rejectsFile"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    missing = [item for item in rejects if item["reason"] == "media_missing"]
    assert len(missing) == 1
    item = missing[0]
    assert item["sample_id"] == "vk_005"
    assert item["source_reference"]["video"] == "media/missing_video.mp4"
    # 拒绝记录不会伪造出媒体文件
    assert not (Path(result["runDir"]) / "media" / "vk_005").exists()
    for record_line in Path(result["outputFile"]).read_text(encoding="utf-8").splitlines():
        if record_line.strip():
            assert json.loads(record_line)["sample_id"] != "vk_005"


def test_run_leaves_no_orphan_files_and_reclaims_rejected_media(tmp_path):
    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_orphan",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    run_dir = Path(result["runDir"])
    # vk_006 的 broken.mp4 已搬进来但解码失败：媒体与帧都必须被回收
    assert not (run_dir / "media" / "vk_006").exists()
    assert not (run_dir / "05_frames" / "vk_006").exists()

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    listed = {item["path"] for item in manifest["artifacts"]}
    # [v4.2] 血缘/阶段归档停用：不再写 lineage.json / stage_context.json
    sidecar = {"manifest.json", "run.log"}
    actual = {path.relative_to(run_dir).as_posix()
              for path in run_dir.rglob("*") if path.is_file()}
    assert listed <= actual, sorted(listed - actual)
    assert actual - listed == sidecar, sorted(actual - listed)

    stats = result["statistics"]
    assert stats["artifact_files"] == len(listed) + 1
    assert stats["media_copied"] == 6
    expected_bytes = sum((run_dir / item["path"]).stat().st_size
                         for item in manifest["artifacts"] if item["role"] == "media")
    assert stats["media_bytes"] == expected_bytes


def test_rejected_sample_does_not_delete_shared_safe_id_artifacts(tmp_path):
    """两个不同 sample_id 归一到同一 safe_id 时，拒绝一方不得删掉另一方产物。"""
    source = tmp_path / "source"
    (source / "media").mkdir(parents=True)
    for name in ("vid_cfr_2s.mp4", "broken.mp4"):
        (source / "media" / name).write_bytes(
            (SAMPLE_DIR / "media" / name).read_bytes())
    entry = source / "demo.jsonl"
    entry.write_text("\n".join([
        json.dumps({"sample_id": "样本_ok", "video": "media/vid_cfr_2s.mp4"},
                   ensure_ascii=False),
        json.dumps({"sample_id": "ok", "video": "media/broken.mp4"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")

    result = core.run(str(entry), "vk_ut_share", config_path=str(CONFIG_PATH),
                      output_root=str(tmp_path / "out"))
    # 两个 sample_id 归一到同一 safe_id 目录
    assert vp.safe_component("样本_ok") == vp.safe_component("ok") == "ok"
    assert result["statistics"]["success"] == 1
    assert result["statistics"]["rejected"] == 1
    check = core.verify_media_references(result["outputFile"], result["runDir"])
    assert check["ok"], check


def test_run_empty_input_is_explicit(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    result = core.run(str(empty), "vk_ut_empty",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["conservation_ok"] is True
    code, message, data = core.build_response(result, True)
    assert code == 0 and "empty_input" in message
    assert data["outputFile"]["entrypoint"] == "05_frame_index.jsonl"


def test_run_all_bad_lines_fails_without_lineage(tmp_path):
    """[v4.2] 血缘停用：失败不再写 lineage.json；失败信封 data={"status": "failed"}。"""
    bad = tmp_path / "allbad.jsonl"
    bad.write_text("{不是 JSON\n[1, 2]\n", encoding="utf-8")
    result = core.run(str(bad), "vk_ut_allbad",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert result["statistics"]["error"] == "invalid_input_records"
    assert "lineage" not in result and "lineageFile" not in result
    assert not (Path(result["runDir"]) / "lineage.json").exists()
    code, _message, data = core.build_response(result, False)
    assert code == 1 and data == {"status": "failed"}


def test_run_invalid_encoding_fails_loudly(tmp_path):
    broken = tmp_path / "not_utf8.jsonl"
    broken.write_bytes(b"\xff\xfe{\"sample_id\": \"x\"}\n")
    result = core.run(str(broken), "vk_ut_encoding",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert result["statistics"]["error"] == "invalid_encoding"


def test_run_does_not_overwrite_previous_run(tmp_path):
    out = tmp_path / "out"
    first = core.run(str(SAMPLE_DIR / "demo_bad.jsonl"), "vk_ut_repeat",
                     config_path=str(CONFIG_PATH), output_root=str(out))
    second = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_repeat",
                      config_path=str(CONFIG_PATH), output_root=str(out))
    assert first["runId"] == "run001" and second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


def test_build_response_unified_regardless_of_is_last(tmp_path):
    """[v4.2] 形态二统一响应：is_last 不再改变形态，去 lineageFile、始终 outputFile。"""
    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_mid",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    for is_last in (False, True):
        code, _message, data = core.build_response(result, is_last)
        assert code == 0
        assert set(data) == {"operator", "status", "outputFile", "response_mode", "result"}
        assert data["response_mode"] == "inline"
        assert data["operator"] == "extract_video_keyframe"
        assert data["status"] == "success"
        assert data["outputFile"]["entrypoint"] == "05_frame_index.jsonl"
        assert data["result"]["status"] == 1
        assert data["result"]["process_status"] == "partial"


def test_build_response_file_object_restorable(tmp_path):
    from utils.transport import restore_file_object

    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_last",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    code, _message, data = core.build_response(result, True)
    assert code == 0

    destination = tmp_path / "restored"
    entry = restore_file_object(data["outputFile"], destination)
    check = core.verify_media_references(entry, destination)
    assert check["ok"], check


# --------------------------------------------------------------------------- #
# 响应上限（v1.1.1）：默认 256 MiB、超限落盘 ZIP 路径引用、打包提前失败
# --------------------------------------------------------------------------- #

def test_default_response_limit_is_256_mib():
    """默认上限 256 MiB（原 64 MiB）：base64 膨胀后仍能内联 138 条实跑的 83 MB 产物。"""
    from utils.schemas import ServerConfig
    from utils.transport import DEFAULT_MAX_RESPONSE_BYTES

    assert DEFAULT_MAX_RESPONSE_BYTES == 256 * 1024 * 1024
    assert ServerConfig().max_response_bytes == 256 * 1024 * 1024
    shipped = (Path(__file__).resolve().parents[1] / "configs" / "config.yaml").read_text(
        encoding="utf-8")
    assert "max_response_bytes: 268435456" in shipped


def test_build_response_falls_back_to_zip_reference_when_over_limit(tmp_path):
    """超限不再整单判失败：落盘 <runDir>_form2.zip 并返回形态一同样的路径引用。"""
    import zipfile

    result = core.run(str(SAMPLE_DIR / "demo.jsonl"), "vk_ut_fallback",
                      config_path=str(CONFIG_PATH), output_root=str(tmp_path / "out"))
    code, message, data = core.build_response(result, True, max_response_bytes=1024)
    assert code == 0, message
    assert data["response_mode"] == "path-reference"
    assert data["response_fallback"] == "output_too_large"
    assert data["response_limit_bytes"] == 1024
    assert data["status"] == "success"

    reference = data["outputFile"]
    assert reference["mode"] == "path-reference"
    assert reference["format"] == "crossmodal-files-v1"
    assert reference["entrypoint"] == "05_frame_index.jsonl"
    assert reference["files"] and "05_frame_index.jsonl" in reference["files"]
    zip_path = Path(reference["zip"])
    assert zip_path.is_file() and zip_path.name.endswith("_form2.zip")
    assert reference["zipBytes"] == zip_path.stat().st_size
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "05_frame_index.jsonl" in names and "manifest.json" in names
    # 路径引用指向真实运行目录，产物完整可读
    entry = Path(reference["runDir"]) / reference["entrypoint"]
    assert entry.is_file()
    check = core.verify_media_references(entry, Path(reference["runDir"]))
    assert check["ok"], check


def test_pack_file_object_aborts_before_reading_oversized_media(tmp_path, monkeypatch):
    """超限时在读取大文件之前就失败，不再把几百 MB 帧序列读进内存。"""
    from utils import transport
    from utils.transport import OutputTooLargeError

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "05_frame_index.jsonl").write_text('{"sample_id": "s1"}\n', encoding="utf-8")
    (run_dir / "05_frames").mkdir()
    (run_dir / "05_frames" / "big.jpg").write_bytes(b"\0" * (2 << 20))

    encoded = []
    real_encode = transport.encode_file

    def spy(relative, path):
        encoded.append(relative)
        return real_encode(relative, path)

    monkeypatch.setattr(transport, "encode_file", spy)
    with pytest.raises(OutputTooLargeError) as excinfo:
        transport.pack_file_object(
            run_dir, "05_frame_index.jsonl",
            ["05_frame_index.jsonl", "05_frames/big.jpg"], max_response_bytes=1 << 20)
    assert encoded == ["05_frame_index.jsonl"]          # 大文件没有被读取
    assert "05_frames/big.jpg" in str(excinfo.value)
    assert "未读取该文件" in str(excinfo.value)


def test_pack_file_object_respects_limit_under_threshold(tmp_path):
    """限额内的正常打包不受提前失败影响，且累计尺寸判定与实际序列化一致。"""
    from utils.transport import FILE_OBJECT_FORMAT, pack_file_object

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "05_frame_index.jsonl").write_text('{"sample_id": "中文"}\n', encoding="utf-8")
    payload = pack_file_object(run_dir, "05_frame_index.jsonl",
                               ["05_frame_index.jsonl"], max_response_bytes=1 << 20)
    assert payload["format"] == FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "05_frame_index.jsonl"


def test_cli_returns_zero_on_partial_and_one_on_failure(tmp_path, capsys):
    out = tmp_path / "out"
    assert core.main(["--input", str(SAMPLE_DIR / "demo.jsonl"), "--unique-id", "vk_ut_cli",
                      "--output-root", str(out), "--quiet"]) == 0
    capsys.readouterr()

    bad = tmp_path / "allbad.jsonl"
    bad.write_text("{不是 JSON\n", encoding="utf-8")
    assert core.main(["--input", str(bad), "--unique-id", "vk_ut_cli_bad",
                      "--output-root", str(out), "--quiet"]) == 1
    capsys.readouterr()


def test_cli_summary_reports_real_data_shape(tmp_path, capsys):
    out = tmp_path / "out"
    assert core.main(["--input", str(SAMPLE_DIR / "demo.jsonl"), "--unique-id", "vk_ut_shape",
                      "--output-root", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out.strip())
    # [v4.2] 统一响应：两种模式 dataShape 一致，无 lineageFile
    expected_shape = {"operator": "str", "status": "str", "outputFile": "object",
                      "response_mode": "str", "result": "object"}
    assert summary["dataShape"] == expected_shape

    assert core.main(["--input", str(SAMPLE_DIR / "demo.jsonl"), "--unique-id", "vk_ut_shape",
                      "--output-root", str(out), "--is-last"]) == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["dataShape"] == expected_shape


def test_cli_rejects_unknown_override(tmp_path, capsys):
    out = tmp_path / "out"
    assert core.main(["--input", str(SAMPLE_DIR / "demo.jsonl"), "--unique-id", "vk_ut_cli_bad_cfg",
                      "--output-root", str(out), "--override", "unknown_key=1"]) == 1
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["code"] == 1
