"""算子 7 业务与 run 集成测试（TECHNICAL_DESIGN §5 用例表 + 公共边界用例）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402
from tests import fixtures  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils.coordination import (  # noqa: E402
    RecordError,
    UnitConfigError,
    build_semantic_unit,
    build_unit,
    media_references,
    safe_stem,
)

UNIT_CONFIG = {
    "unit_policy": "per_sample",
    "max_av_duration_diff_ratio": 0.2,
    "av_mismatch_action": "review",
    "require_existing_media": False,
    "validate_frame_timestamps": True,
}


def read_jsonl(path: Path):
    return [json.loads(line) for line in
            io_utils.iter_decoded_lines(path, "utf-8") if line.strip()]


# ---------------------------------------------------------------- 纯函数用例

def test_build_unit_no_longer_creates_semantic_unit():
    """[v1.0.1] #8 只做记录级校验+媒体引用收集；semantic_unit 固定 null。"""
    out, meta = build_unit(fixtures.make_record("ti1"), UNIT_CONFIG)
    assert out["semantic_unit"] is None
    assert meta["present_modalities"] == ["text", "image"]
    assert meta["has_audio_source"] is False
    assert meta["media_references"] == ["04_images/ti1.png"]


def test_text_image_fields_are_preserved_and_input_not_mutated():
    """透传：自定义 metadata / 上游 processing 不丢，输入记录不被就地修改。"""
    record = fixtures.make_record("ti1")
    record["metadata"] = {"custom": {"batch": "b-1"}, "tags": ["a", "b"]}
    first = build_semantic_unit(record, UNIT_CONFIG)
    second = build_semantic_unit(record, UNIT_CONFIG)
    assert first == second
    assert first["metadata"] == {"custom": {"batch": "b-1"}, "tags": ["a", "b"]}
    assert first["text"]["token_offsets"] == [[0, 5], [6, 11]]
    assert first["processing"]["03"]["status"] == "success"
    assert "07" not in first["processing"]
    assert "semantic_unit" not in record and "07" not in record["processing"]


def test_image_path_string_is_no_longer_rejected():
    """图片仍为路径字符串（缺 04 元数据）不应拒掉整条记录。"""
    record = {"sample_id": "x", "text": "hello", "image": "04_images/x.png"}
    out = build_semantic_unit(record, UNIT_CONFIG)
    assert out["semantic_unit"] is None
    assert out["image"] == "04_images/x.png"
    assert media_references(out) == ["04_images/x.png"]


def test_media_metadata_is_no_longer_validated():
    """视频帧乱序/越界、音频缺时长等上游缺陷不再由 #8 判失败。"""
    record = fixtures.make_av_record(
        "v-bad", 1.0, 1.0,
        video_frames=[{"timestamp": 2.0, "path": "f.png"},
                      {"timestamp": -0.5, "path": "g.png"}],
        audio_segments=[{"start": 0.0, "end": 9.0}])
    record["video"].pop("duration")
    record["audio"].pop("duration")
    out = build_semantic_unit(record, UNIT_CONFIG)
    assert out["semantic_unit"] is None
    # 记录级 processing["07"] 由 run（ASR 结果）写入，纯函数不臆造流程态
    assert "07" not in (out.get("processing") or {})


def test_av_duration_mismatch_no_longer_flags_or_rejects():
    """#8 不再做音视频配对，时长差异只透传，不产生 review/reject。"""
    record = fixtures.make_av_record("av2", 1.0, 0.5)
    out = build_semantic_unit(record, dict(UNIT_CONFIG, av_mismatch_action="reject"))
    assert out["video"]["duration"] == 1.0 and out["audio"]["duration"] == 0.5
    assert out["semantic_unit"] is None


def test_missing_alignment_and_qc_are_initialized():
    record = fixtures.make_record("ti2")
    out = build_semantic_unit(record, UNIT_CONFIG)
    assert out["alignment"] == {"whole": None, "local": []}
    assert out["qc"] == {}


def test_existing_alignment_and_qc_are_not_overwritten():
    record = fixtures.make_record("ti3")
    record["alignment"] = {"whole": {"offset": 0, "length": 5}, "local": [{"start": 0}]}
    record["qc"] = {"blur_pass": False}
    out = build_semantic_unit(record, UNIT_CONFIG)
    assert out["alignment"]["whole"] == {"offset": 0, "length": 5}
    assert out["qc"] == {"blur_pass": False}


def test_has_audio_source_covers_audio_and_video():
    from utils.coordination import has_audio_source

    assert has_audio_source({"audio": {"path": "a.wav"}}) is True
    assert has_audio_source({"video": {"path": "a.mp4"}}) is True
    assert has_audio_source({"video": "media/a.mp4"}) is True
    assert has_audio_source({"text": "x", "image": {"path": "a.png"}}) is False


@pytest.mark.parametrize("record", [
    {"sample_id": "", "text": "x"},
    {"sample_id": 7, "text": "x"},
    {"text": "no id"},
])
def test_invalid_records_rejected_with_reason(record):
    with pytest.raises(RecordError) as excinfo:
        build_semantic_unit(record, UNIT_CONFIG)
    assert excinfo.value.reason == "invalid_record"
    assert excinfo.value.message


def test_media_references_collects_originals_frames_and_segments():
    record = fixtures.make_av_record(
        "ref1", 1.0, 1.0,
        video_frames=[{"timestamp": 0.0, "path": "05_frames/ref1/f0.png"}],
        audio_segments=[{"start": 0.0, "end": 0.5, "path": "06_audio/ref1/s0.wav"}])
    references = media_references(record)
    assert "media/original/ref1.mp4" in references
    assert "05_frames/ref1/f0.png" in references
    assert "06_audio/ref1/s0.wav" in references
    assert len(references) == len(set(references))


@pytest.mark.parametrize("config", [
    {"unit_policy": "per_pair"},
    {"av_mismatch_action": "drop"},
    {"max_av_duration_diff_ratio": 1.5},
    {"max_av_duration_diff_ratio": float("nan")},
    {"require_existing_media": "yes"},
    {"unknown_key": 1},
])
def test_bad_business_config_rejected(config):
    record = fixtures.make_record("cfg1")
    with pytest.raises(UnitConfigError):
        build_semantic_unit(record, config)


def test_safe_stem_is_filesystem_safe():
    assert safe_stem("a/b:c*1") == "a_b_c_1"
    assert safe_stem("") == "sample"


# ---------------------------------------------------------------- run 集成用例

def _run_upload(tmp_path: Path, records, media=None, unique_id: str = "cm07_unit",
                overrides=None) -> dict:
    """阶段首形态：素材在 ``OUTPUT_DIR/<uniqueId>/input``，inputFile 缺省。"""
    out = tmp_path / "out"
    fixtures.write_task(out / unique_id, records, media=media)
    return inference.run(None, unique_id, output_root=str(out), overrides=overrides)


def _run_fixture(tmp_path: Path, unique_id: str = "cm07_unit", overrides=None) -> dict:
    """用 dataset/sample 生成的完整 fixture 跑一次上传态任务。"""
    out = tmp_path / "out"
    fixtures.copy_sample_into(fixtures.upload_dir(out, unique_id))
    return inference.run(None, unique_id, output_root=str(out), overrides=overrides)


def _run_chained(tmp_path: Path, unique_id: str = "cm07_unit",
                 with_context: bool = True) -> dict:
    """阶段内串联形态：inputFile 指向同 uniqueId 的上游运行目录。"""
    out = tmp_path / "out"
    upstream = fixtures.upstream_dir(out, unique_id)
    fixtures.build_fixture(upstream)
    if with_context:
        fixtures.write_stage_context(upstream, unique_id)
    return inference.run(str(upstream / "demo.jsonl"), unique_id, output_root=str(out))


def test_run_fixture_end_to_end(tmp_path):
    """完整 fixture：5 条记录全部有去向，媒体与旁路随包搬运。

    转写默认关闭：4 条带音频来源的记录 ``skipped``，纯文本记录 ``not_applicable``；
    不再生成 ``semantic_unit``，并固定写出空的待补清单。
    """
    result = _run_fixture(tmp_path)
    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["input"] == 5 and stats["output"] == 5 and stats["rejected"] == 0
    assert stats["transcribed"] == 0
    assert stats["transcription_skipped"] == 2 and stats["not_applicable"] == 3
    assert stats["no_speech"] == 0 and stats["transcription_failed"] == 0
    assert stats["supplement_requests"] == 0
    assert stats["conservation_ok"] is True
    assert stats["media_missing"] == 0 and stats["sidecar_files"] == 1

    run_dir = Path(result["runDir"])
    records = read_jsonl(Path(result["outputFile"]))
    assert len(records) == 5
    by_id = {record["sample_id"]: record for record in records}
    for record in records:
        assert record["semantic_unit"] is None
        assert "semantic_units" not in record
    assert by_id["demo_07_v1_en"]["processing"]["07"]["process_status"] == "skipped"
    assert by_id["demo_07_v1_en"]["processing"]["07"]["metrics"]["asr_units"] == 0
    assert by_id["demo_07_t1_zh"]["processing"]["07"]["process_status"] == "not_applicable"
    assert by_id["demo_07_t1_zh"]["processing"]["07"]["reason"] == "no_audio_input"
    assert by_id["demo_07_v2_en"]["processing"]["07"]["status"] == 1

    # 记录中所有媒体引用都能在 run 目录内打开
    for record in records:
        for relative in media_references(record):
            assert (run_dir / relative).is_file(), relative

    # 待补清单总是产出（本批为空），且登记在 artifacts 的 supplement 角色下
    supplement = run_dir / inference.SUPPLEMENT_NAME
    assert supplement.is_file() and supplement.read_text(encoding="utf-8") == ""

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "07_candidates.jsonl"
    listed = {item["path"] for item in manifest["artifacts"]}
    assert "07_rejects.jsonl" in listed and "03_rejects.jsonl" in listed
    assert inference.SUPPLEMENT_NAME in listed
    roles = {item["path"]: item["role"] for item in manifest["artifacts"]}
    assert roles[inference.SUPPLEMENT_NAME] == "supplement"
    for item in manifest["artifacts"]:
        assert (run_dir / item["path"]).is_file(), item


def test_run_counts_bad_lines_and_rejects_without_losing_them(tmp_path):
    """空行/坏 JSON/非对象/缺 ID/重复 ID/缺媒体都不静默消失。"""
    media = {"04_images/ok.png": fixtures.PNG_BYTES}
    records = [
        fixtures.make_record("ok"),
        "",                                            # 空行
        "{not json",                                    # 坏 JSON
        "[1, 2, 3]",                                    # 非对象
        {"text": "no id"},                              # 缺 sample_id
        fixtures.make_record("ok"),                     # 重复 sample_id
        fixtures.make_record("missing", image={
            "path": "04_images/absent.png", "width": 8, "height": 8}),  # 缺媒体
        fixtures.make_record("escape", image={
            "path": "../../outside.png", "width": 8, "height": 8}),     # 路径穿越
    ]
    result = _run_upload(tmp_path, records, media=media)
    stats = result["statistics"]

    assert result["status"] == "partial"
    assert stats["input"] == 7 and stats["blank_lines"] == 1
    assert stats["output"] == 1 and stats["rejected"] == 6
    assert stats["conservation_ok"] is True
    assert stats["reject_reasons"]["invalid_json"] == 1
    assert stats["reject_reasons"]["invalid_record"] == 2
    assert stats["reject_reasons"]["duplicate_sample_id"] == 1
    assert stats["reject_reasons"]["media_missing"] == 1
    assert stats["reject_reasons"]["unsupported_media_path"] == 1

    rejects = read_jsonl(Path(result["rejectFile"]))
    assert len(rejects) == 6
    by_reason = {item["reason"]: item for item in rejects}
    assert by_reason["invalid_json"]["line"] == 3           # 保留行号
    assert by_reason["media_missing"]["evidence"]["media_errors"][0]["path"] == \
        "04_images/absent.png"
    assert by_reason["duplicate_sample_id"]["record"]["sample_id"] == "ok"
    assert by_reason["unsupported_media_path"]["record"]["sample_id"] == "escape"


def test_run_media_missing_does_not_copy_partial_media(tmp_path):
    """被拒绝记录的媒体不进入 run 目录，避免残留孤儿文件。"""
    record = fixtures.make_record("multi", image={
        "path": "04_images/bad.png", "width": 8, "height": 8})
    record["image"]["original_path"] = "media/original/missing.png"
    result = _run_upload(tmp_path, [record], media={"04_images/bad.png": fixtures.PNG_BYTES})
    assert result["statistics"]["rejected"] == 1
    run_dir = Path(result["runDir"])
    assert not (run_dir / "04_images" / "bad.png").exists()


def test_run_empty_input_is_success_with_empty_flag(tmp_path):
    result = _run_upload(tmp_path, [])
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["input"] == 0
    assert Path(result["outputFile"]).read_text(encoding="utf-8") == ""


def test_run_requires_existing_media(tmp_path):
    """run 强制存在性校验：config 关闭会直接报配置错误。"""
    with pytest.raises(inference.ConfigError) as excinfo:
        _run_upload(tmp_path, [fixtures.make_record("x")],
                    media={"04_images/x.png": fixtures.PNG_BYTES},
                    overrides={"require_existing_media": False})
    assert "require_existing_media" in str(excinfo.value)


def test_run_rejects_input_file_outside_task(tmp_path):
    """跨任务读取必须被拒绝：inputFile 越出任务根。"""
    outside = tmp_path / "elsewhere" / "demo.jsonl"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(inference.InputError):
        inference.run(str(outside), "cm07_unit", output_root=str(tmp_path / "out"))


def test_run_fails_on_missing_input(tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(None, "cm07_missing", output_root=str(tmp_path / "out"))


def test_failed_run_leaves_no_half_product_directory(tmp_path):
    """失败且无产物时不留下半成品 run 目录（需先释放 run.log 句柄）。"""
    out = tmp_path / "out"
    with pytest.raises(inference.InputError):
        inference.run(None, "cm07_fail", output_root=str(out))
    assert not (out / "cm07_fail" / "multimodal_preprocessing" / "run001").exists()


def test_run_isolated_between_tasks_and_repeatable(tmp_path):
    """任务隔离 + 重复执行：各自独立 run 目录，不覆盖彼此。"""
    first = _run_fixture(tmp_path, unique_id="cm07_a")
    repeat = _run_fixture(tmp_path, unique_id="cm07_a")
    other = _run_fixture(tmp_path, unique_id="cm07_b")

    assert first["runId"] == "run001" and repeat["runId"] == "run002"
    assert other["runId"] == "run001"
    assert first["runDir"] != other["runDir"]
    assert Path(first["outputFile"]).is_file() and Path(repeat["outputFile"]).is_file()


@pytest.mark.skip(reason="v4.2 血缘输出已停用：中间/阶段血缘形态函数已注释存档")
def test_intermediate_and_stage_payload_shapes(tmp_path):
    """中间只回当前节点血缘；阶段末回完整数组且不含本地绝对路径。"""
    result = _run_chained(tmp_path)
    node = inference.build_intermediate_lineage(result)
    assert node["operator"] == "multimodal_preprocessing"
    assert node["outputFile"].endswith("07_candidates.jsonl")

    payload = inference.build_stage_payload(result)
    assert [item["operator"] for item in payload] == [
        "text_cleaning", "image_standardization", "video_keyframe_extraction",
        "audio_normalization", "multimodal_preprocessing"]
    assert all("outputFile" not in item for item in payload)
    for item in payload:
        for artifact in item.get("artifacts", []):
            assert artifact["path"] == Path(artifact["path"]).name


@pytest.mark.skip(reason="v4.2 血缘/阶段归档输出已停用：不再写 stage_context.json")
def test_stage_lineage_continues_from_upstream_context(tmp_path):
    """阶段血缘连续性：上游 stage_context 的节点被继承并追加本节点。"""
    result = _run_chained(tmp_path)
    context = json.loads((Path(result["runDir"]) / "stage_context.json")
                         .read_text(encoding="utf-8"))
    assert len(context["stage_lineage"]) == 5
    assert context["stage_lineage"][-1]["operator"] == "multimodal_preprocessing"
    lineage = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))
    assert len(lineage) == 5


@pytest.mark.skip(reason="v4.2 血缘输出已停用：run() 不再返回 stageLineage/priorLineage")
def test_stage_lineage_starts_empty_without_upstream_context(tmp_path):
    """没有上游上下文时本阶段从空数组开始，不虚构历史。"""
    result = _run_chained(tmp_path, with_context=False)
    assert len(result["stageLineage"]) == 1
    assert result["priorLineage"] == []


def test_config_loading_rules(tmp_path):
    """配置：未知字段、枚举、越界、部署身份改写、缺文件都报错。"""
    base = inference.default_config_path()
    cfg = inference.load_config(str(base))
    assert cfg.operator.name == "multimodal_preprocessing" and cfg.stage_id == "S2"
    assert cfg.max_av_duration_diff_ratio == 0.2

    overridden = inference.load_config(str(base), {"av_mismatch_action": "reject"})
    assert overridden.av_mismatch_action == "reject"
    assert overridden.unit_policy == "per_sample"

    for bad in ({"operator": {"name": "x", "version": "1"}}, {"stage_id": "S1"},
                {"av_mismatch_action": "skip"}, {"max_av_duration_diff_ratio": 2.0},
                {"max_av_duration_diff_ratio": "0.5"}, {"nope": 1}):
        with pytest.raises(inference.ConfigError):
            inference.load_config(str(base), bad)

    with pytest.raises(inference.ConfigError):
        inference.load_config(str(tmp_path / "absent.yaml"))


def test_operator_config_env_var_is_honored(tmp_path, monkeypatch):
    """``OPERATOR_CONFIG`` 环境变量可指定部署配置路径（说明书 §2.2）。"""
    assert inference.default_config_path().name == "config.yaml"
    custom = tmp_path / "custom.yaml"
    custom.write_text(inference.default_config_path().read_text(encoding="utf-8"),
                      encoding="utf-8")
    monkeypatch.setenv("OPERATOR_CONFIG", str(custom))
    assert inference.default_config_path() == custom
    assert inference.load_config().operator.name == "multimodal_preprocessing"


# ---------------------------------------------------------------- CLI 边界

def test_cli_runs_with_external_fixture(tmp_path):
    """CLI 允许读取任务根外的本地 fixture，产物落在对应任务 run 目录。"""
    sample = fixtures.build_fixture(tmp_path / "sample")
    out = tmp_path / "out"
    code = inference.main(["--input", str(sample / "demo.jsonl"),
                           "--unique-id", "cm07_cli", "--output-root", str(out),
                           "--quiet"])
    assert code == 0
    produced = out / "cm07_cli" / "multimodal_preprocessing" / "run001" / "07_candidates.jsonl"
    assert produced.is_file()
    assert len(read_jsonl(produced)) == 5


def test_run_default_rejects_external_input_but_explicit_flag_allows(tmp_path):
    """run 默认严格（HTTP 语义）；只有显式打开时才允许任务根外输入。"""
    sample = fixtures.build_fixture(tmp_path / "sample")
    entry = sample / "demo.jsonl"
    with pytest.raises(inference.InputError):
        inference.run(str(entry), "cm07_strict", output_root=str(tmp_path / "out"))

    resolved = inference.resolve_input(tmp_path / "out" / "cm07_cli", str(entry),
                                       allow_external=True)
    assert resolved == entry.resolve()
