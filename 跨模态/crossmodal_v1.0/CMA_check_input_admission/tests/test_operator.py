"""算子 10 业务处理、配置、降级与 CLI 测试。

覆盖（提示词第四节要求）：
正常样例、边界值、空文件、损坏 JSONL、缺字段、错误配置、错误编码、任务隔离、
路径安全、两种 ``isLastOperator`` 返回形态（接口测试覆盖）、以及本算子专项降级/失败路径
（缺描述退回 #9、损坏文件进原始隔离区、授权/模态问题进样本隔离库、退回超限锁定、解码依赖降级）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests import fixtures as fx
from utils import admission as admission_module
from utils.schemas import ConfigError

SAMPLE_ENTRY = fx.SAMPLE_DIR / fx.SAMPLE_ENTRYPOINT


# ------------------------------------------------------------------ 工具
def _run(tmp_path, records, *, unique_id: str = "task_file1", source_name: str = "src",
         **kwargs):
    source = tmp_path / source_name
    entry = fx.write_input_dir(source, records)
    out_root = tmp_path / "output"
    result = inference.run(str(entry), unique_id, output_root=str(out_root),
                           allow_external_input=True, **kwargs)
    return result, out_root, entry


def _lists(result):
    return (fx.read_jsonl(result["outputFile"]),
            fx.read_jsonl(result["returnFile"]),
            fx.read_jsonl(result["originQuarantineFile"]),
            fx.read_jsonl(result["sampleQuarantineFile"]))


def _stats(result):
    return result["statistics"]


# ------------------------------------------------------------------ 正常与统计
def test_normal_record_admitted_with_real_statistics(tmp_path):
    record = fx.make_record("s-ok-1")
    result, _out_root, _entry = _run(tmp_path, [record])

    assert result["status"] == "success"
    assert Path(result["outputFile"]).name == "10_admitted.jsonl"
    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(returned), len(origin), len(sample_quarantine)) == (1, 0, 0, 0)

    stats = _stats(result)
    assert stats["input"] == 1 and stats["valid_records"] == 1 and stats["bad_lines"] == 0
    assert stats["admitted"] == 1
    assert stats["conservation_ok"] is True

    out = admitted[0]
    assert out["sample_id"] == "s-ok-1"
    assert out["admission"]["passed"] is True
    assert out["admission"]["modalities"] == ["text", "image"]
    assert out["admission"]["modality_count"] == 2
    assert out["admission"]["license"] == fx.VALID_LICENSE
    assert out["admission"]["source_id"] == "SRC-A"
    assert out["admission"]["batch_id"] == "BATCH-2026-10"
    assert out["admission"]["description"]["chars"] == len("红色方块在白色桌面上")
    # 只追加自己的命名空间，不覆盖上游结论
    assert out["processing"]["09"]["status"] == "success"
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert out["processing"]["10"]["status"] == 1
    assert out["processing"]["10"]["process_status"] == "success"
    assert out["alignment"] == {"whole": None, "local": []}
    assert out["qc"] == {}
    assert out["metadata"]["image_tags"] == ["红色", "方块"]


def test_lineage_output_is_disabled_and_true_counts(tmp_path):
    """[v4.2] 血缘输出已停用：不再回传 stageLineage，也不再写 lineage.json/stage_context.json。"""
    result, out_root, _entry = _run(tmp_path, [fx.make_record("s-1")])
    assert "stageLineage" not in result
    assert "priorLineage" not in result
    assert "lineageFile" not in result
    assert result["statistics"]["admitted"] == 1
    assert inference.load_config(str(inference.default_config_path())).min_modalities == 2

    run_dir = Path(result["runDir"])
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "run.log").is_file()
    # 输出全部落在 OUTPUT_DIR/<uniqueId>/ 下
    assert Path(result["outputFile"]).resolve().is_relative_to(out_root.resolve())


def test_prior_lineage_history_is_ignored(tmp_path):
    """[v4.2] 血缘停用后，上游 lineage_history.json 不再被读取/拼接。"""
    source = tmp_path / "src"
    entry = fx.write_input_dir(source, [fx.make_record("s-1")])
    (source / "lineage_history.json").write_text(json.dumps([{
        "operator": "candidate_description", "operator_version": "v1.0.0",
        "uniqueId": "stage_f1", "stageId": "S2", "inputFile": "x", "status": "success",
    }], ensure_ascii=False), encoding="utf-8")
    result = inference.run(str(entry), "task_file1", output_root=str(tmp_path / "output"),
                           allow_external_input=True)
    assert "priorLineage" not in result
    assert "stageLineage" not in result
    assert not (Path(result["runDir"]) / "lineage.json").exists()


# ------------------------------------------------------------------ 缺描述 → 退回 #9
def test_missing_description_returns_to_09_not_quarantined(tmp_path):
    record = fx.make_record("s-no-desc", text=None)
    record["text"] = {"content": "", "original": "", "tokens": [], "lang": "zh"}
    result, _root, _entry = _run(tmp_path, [record])

    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(origin), len(sample_quarantine)) == (0, 0, 0)
    assert len(returned) == 1
    entry = returned[0]
    assert entry["reason"] == "description_missing"
    assert entry["label"] == "流程-待补描述"
    assert entry["destination"] == "09_准备候选描述"
    assert entry["action"] == "return"
    assert entry["locked"] is False
    assert entry["sample_id"] == "s-no-desc"
    stats = _stats(result)
    assert stats["returned"] == 1 and stats["returned_description"] == 1
    assert stats["sample_quarantined"] == 0 and stats["origin_quarantined"] == 0
    assert result["status"] == "success"


def test_filename_or_tag_as_description_returns_to_09(tmp_path):
    by_filename = fx.make_record("s-fn", text="img_ok")
    by_source = fx.make_record("s-src", description_source="文件名")
    by_tag = fx.make_record("s-tag", text="红色")
    result, _root, _entry = _run(tmp_path, [by_filename, by_source, by_tag])

    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(origin), len(sample_quarantine)) == (0, 0, 0)
    reasons = {entry["sample_id"]: entry["reason"] for entry in returned}
    assert reasons["s-fn"] == "description_is_filename"
    assert reasons["s-src"] == "description_source_invalid"
    assert reasons["s-tag"] == "description_is_tag"
    assert all(entry["destination"] == "09_准备候选描述" for entry in returned)


def test_return_limit_locks_into_sample_quarantine(tmp_path):
    record = fx.make_record("s-locked", text=None, retry_count=2)
    result, _root, _entry = _run(tmp_path, [record])

    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(returned), len(origin)) == (0, 0, 0)
    assert len(sample_quarantine) == 1
    entry = sample_quarantine[0]
    assert entry["reason"] == "return_limit_exceeded"
    assert entry["label"] == "样本-语义不通"
    assert entry["destination"] == "样本隔离库"
    assert entry["locked"] is True
    assert entry["retry_count"] == 2
    assert _stats(result)["locked"] == 1


def test_return_limit_boundary_respects_override(tmp_path):
    record = fx.make_record("s-limit", text=None, retry_count=1)
    # 默认 max_return_count=2：retry_count=1 仍退回
    _result, _root, _entry = _run(tmp_path, [record], unique_id="task_file1")
    assert len(_lists(_result)[1]) == 1
    # 覆盖为 1：retry_count=1 已达上限 → 锁定
    result, _root2, _entry2 = _run(tmp_path, [record], unique_id="task_file2",
                                   overrides={"max_return_count": 1})
    assert len(_lists(result)[3]) == 1


# ------------------------------------------------------------------ 原始文件问题 → 原始隔离区
def test_broken_and_empty_and_missing_files_go_origin_quarantine(tmp_path):
    records = [
        fx.make_record("s-broken", image="media/img_broken.png"),
        fx.make_record("s-empty", image="media/img_empty.png"),
        fx.make_record("s-missing", image="media/nonexistent.png"),
    ]
    result, _root, _entry = _run(tmp_path, records)
    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(returned), len(sample_quarantine)) == (0, 0, 0)
    assert len(origin) == 3
    labels = {entry["sample_id"]: entry["label"] for entry in origin}
    reasons = {entry["sample_id"]: entry["reason"] for entry in origin}
    assert labels["s-broken"] == "原始文件-损坏"
    assert labels["s-empty"] == "原始文件-空"
    assert labels["s-missing"] == "原始文件-损坏"
    assert reasons["s-broken"] == "file_signature_mismatch"
    assert reasons["s-empty"] == "file_empty"
    assert reasons["s-missing"] == "file_missing"
    assert all(entry["destination"] == "原始隔离区" for entry in origin)
    assert all(entry["label"].startswith("原始文件-") for entry in origin)


def test_format_not_allowed_goes_origin_quarantine(tmp_path):
    record = fx.make_record("s-ext", image="media/note.txt")
    result, _root, _entry = _run(tmp_path, [record])
    origin = _lists(result)[2]
    assert len(origin) == 1
    assert origin[0]["reason"] == "format_not_allowed"
    assert origin[0]["label"] == "原始文件-无法解析"


def test_missing_sample_id_goes_origin_quarantine(tmp_path):
    record = fx.make_record("s-x")
    del record["sample_id"]
    result, _root, _entry = _run(tmp_path, [record])
    origin = _lists(result)[2]
    assert len(origin) == 1
    assert origin[0]["reason"] == "missing_sample_id"
    assert origin[0]["line"] == 1


# ------------------------------------------------------------------ 授权 / 模态 → 样本隔离库
def test_license_not_trainable_goes_sample_quarantine(tmp_path):
    records = [fx.make_record("s-license", license="不可训练"),
               fx.make_record("s-license-2", license="待确认")]
    result, _root, _entry = _run(tmp_path, records)
    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(returned), len(origin)) == (0, 0, 0)
    assert len(sample_quarantine) == 2
    assert all(entry["label"] == "样本-敏感" for entry in sample_quarantine)
    assert {entry["reason"] for entry in sample_quarantine} == {"license_not_trainable"}
    assert all(entry["destination"] == "样本隔离库" for entry in sample_quarantine)


def test_license_missing_goes_sample_quarantine(tmp_path):
    record = fx.make_record("s-no-license", license=None)
    result, _root, _entry = _run(tmp_path, [record])
    sample_quarantine = _lists(result)[3]
    assert len(sample_quarantine) == 1
    assert sample_quarantine[0]["reason"] == "license_missing"
    assert sample_quarantine[0]["label"] == "样本-敏感"


def test_single_modality_goes_sample_quarantine(tmp_path):
    record = fx.make_record("s-single", image=None)
    result, _root, _entry = _run(tmp_path, [record])
    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(returned), len(origin)) == (0, 0, 0)
    assert len(sample_quarantine) == 1
    assert sample_quarantine[0]["reason"] == "modality_insufficient"
    assert sample_quarantine[0]["label"] == "样本-语义不通"
    assert _stats(result)["modality_counts"]["1"] == 1


def test_min_modalities_boundary_and_validation(tmp_path):
    two_modalities = fx.make_record("s-2m")
    result, _root, _entry = _run(tmp_path, [two_modalities], unique_id="task_strict",
                                 overrides={"min_modalities": 3})
    assert len(_lists(result)[3]) == 1  # 2 < 3 → 样本隔离

    result2, _root2, _entry2 = _run(tmp_path, [two_modalities], unique_id="task_relaxed",
                                    overrides={"min_modalities": 2})
    assert len(_lists(result2)[0]) == 1

    four = fx.make_record("s-4m", video="media/video_ok.mp4", audio="media/audio_ok.wav")
    result3, _root3, _entry3 = _run(tmp_path, [four], unique_id="task_four",
                                    overrides={"min_modalities": 4})
    assert _lists(result3)[0][0]["admission"]["modality_count"] == 4

    with pytest.raises(ConfigError):
        _run(tmp_path, [two_modalities], unique_id="task_bad1",
             overrides={"min_modalities": 1})
    with pytest.raises(ConfigError):
        _run(tmp_path, [two_modalities], unique_id="task_bad2",
             overrides={"min_modalities": 5})


def test_required_license_cannot_be_overridden(tmp_path):
    with pytest.raises(ConfigError):
        _run(tmp_path, [fx.make_record("s-1")], unique_id="task_lic",
             overrides={"required_license": "可训练"})


def test_missing_source_or_batch_returns_to_upstream(tmp_path):
    records = [fx.make_record("s-no-src", source_id=None),
               fx.make_record("s-no-batch", batch_id=None)]
    result, _root, _entry = _run(tmp_path, records)
    admitted, returned, origin, sample_quarantine = _lists(result)
    assert (len(admitted), len(origin), len(sample_quarantine)) == (0, 0, 0)
    assert {entry["reason"] for entry in returned} == {"missing_source_id", "missing_batch_id"}
    assert all(entry["destination"] == "01_接入多模态批次" for entry in returned)
    assert all(entry["label"] == "流程-待补登记" for entry in returned)


# ------------------------------------------------------------------ 描述长度边界
def test_description_length_boundary(tmp_path):
    short = fx.make_record("s-short", text="红色方块")
    ok = fx.make_record("s-ok", text="红色方块在")
    long = fx.make_record("s-long", text="描" * 501)
    result, _root, _entry = _run(tmp_path, [short, ok, long])
    admitted, returned, _origin, _sample = _lists(result)
    assert [record["sample_id"] for record in admitted] == ["s-ok"]
    reasons = {entry["sample_id"]: entry["reason"] for entry in returned}
    assert reasons["s-short"] == "description_too_short"
    assert reasons["s-long"] == "description_too_long"


# ------------------------------------------------------------------ 坏行 / 空输入 / 编码
def test_bad_json_lines_and_invalid_records_are_recorded(tmp_path):
    good = json.dumps(fx.make_record("s-good"), ensure_ascii=False)
    raw_lines = [good, "{not-json", "[1,2,3]", "", json.dumps(fx.make_record("s-good-2"),
                                                              ensure_ascii=False)]
    source = tmp_path / "src"
    entry = fx.write_input_dir(source, raw_lines=raw_lines)
    result = inference.run(str(entry), "task_bad_lines", output_root=str(tmp_path / "output"),
                           allow_external_input=True)

    stats = _stats(result)
    assert stats["input_lines"] == 5
    assert stats["blank_lines"] == 1
    assert stats["input"] == 4
    assert stats["bad_lines"] == 2
    assert stats["valid_records"] == 2
    assert stats["admitted"] == 2
    # origin_quarantined 只统计“有效记录”被原始隔离的条数；坏行另计 origin_entries
    assert stats["origin_quarantined"] == 0
    assert stats["origin_entries"] == 2
    assert stats["conservation_ok"] is True
    origin = fx.read_jsonl(result["originQuarantineFile"])
    assert [entry["line"] for entry in origin] == [2, 3]
    assert [entry["reason"] for entry in origin] == ["invalid_json", "invalid_record"]
    assert result["status"] == "partial"


def test_empty_input_is_reported_not_silently_ok(tmp_path):
    source = tmp_path / "src"
    entry = fx.write_input_dir(source, [])
    result = inference.run(str(entry), "task_empty", output_root=str(tmp_path / "output"),
                           allow_external_input=True)
    stats = _stats(result)
    assert stats["empty_input"] is True
    assert stats["input"] == 0 and stats["admitted"] == 0
    assert stats["conservation_ok"] is True
    assert result["status"] == "success"
    assert "empty_input" in result["message"]
    assert Path(result["outputFile"]).is_file()
    assert Path(result["outputFile"]).read_text(encoding="utf-8") == ""


def test_non_utf8_input_fails_structurally(tmp_path):
    source = tmp_path / "src"
    payload = json.dumps(fx.make_record("s-gbk"), ensure_ascii=False).encode("gbk")
    entry = fx.write_input_dir(source, [], entry_bytes=payload)
    with pytest.raises(inference.InputError):
        inference.run(str(entry), "task_gbk", output_root=str(tmp_path / "output"),
                      allow_external_input=True)


# ------------------------------------------------------------------ 任务隔离与路径安全
def test_two_unique_ids_are_isolated(tmp_path):
    result_a, out_root, _entry = _run(tmp_path, [fx.make_record("s-a")], unique_id="task_a_f1")
    result_b, _root2, _entry2 = _run(tmp_path, [fx.make_record("s-b")], unique_id="task_b_f1",
                                     source_name="src2")
    task_a = out_root / "task_a_f1"
    task_b = out_root / "task_b_f1"
    assert Path(result_a["outputFile"]).is_relative_to(task_a)
    assert Path(result_b["outputFile"]).is_relative_to(task_b)
    assert "s-a" in Path(result_a["outputFile"]).read_text(encoding="utf-8")
    assert "s-b" not in Path(result_a["outputFile"]).read_text(encoding="utf-8")
    assert "s-b" in Path(result_b["outputFile"]).read_text(encoding="utf-8")


def test_input_file_outside_task_is_rejected(tmp_path):
    source = tmp_path / "src"
    entry = fx.write_input_dir(source, [fx.make_record("s-1")])
    with pytest.raises(inference.InputError):
        inference.run(str(entry), "task_scope", output_root=str(tmp_path / "output"))


@pytest.mark.parametrize("bad_id", ["", "  ", "../escape", "a/b", "a\\b", "a:b", "..",
                                    "con", "C:\\abs", "task."])
def test_invalid_unique_id_rejected(tmp_path, bad_id):
    from utils import io as io_utils

    with pytest.raises(ValueError):
        io_utils.validate_unique_id(bad_id)
    source = tmp_path / "src2"
    entry = fx.write_input_dir(source, [fx.make_record("s-1")])
    with pytest.raises(ValueError):
        inference.run(str(entry), bad_id, output_root=str(tmp_path / "output"),
                      allow_external_input=True)


# ------------------------------------------------------------------ 重复 sample_id / 配置校验
def test_duplicate_sample_id_is_counted_and_flagged(tmp_path):
    records = [fx.make_record("s-dup"), fx.make_record("s-dup")]
    result, _root, _entry = _run(tmp_path, records)
    admitted = _lists(result)[0]
    assert len(admitted) == 2
    assert _stats(result)["duplicates"] == 1
    assert admitted[1]["admission"]["duplicate"] is True


def test_config_rejects_unknown_keys_and_bad_types(tmp_path):
    record = fx.make_record("s-1")
    for bad in ({"unknown_key": 1}, {"min_description_chars": "5"},
                {"file_check_mode": "hack"}, {"allow_degraded": "yes"},
                {"required_license": "x"}):
        with pytest.raises(ConfigError):
            _run(tmp_path, [record], unique_id="task_cfg_%d" % len(bad),
                 overrides=bad)
    with pytest.raises(ConfigError):
        _run(tmp_path, [record], unique_id="task_cfg_str", overrides=[])  # type: ignore[arg-type]


def test_missing_config_file_fails(tmp_path):
    source = tmp_path / "src"
    entry = fx.write_input_dir(source, [fx.make_record("s-1")])
    with pytest.raises(ConfigError):
        inference.run(str(entry), "task_nocfg", config_path=str(tmp_path / "nope.yaml"),
                      output_root=str(tmp_path / "output"), allow_external_input=True)


# ------------------------------------------------------------------ 解码模式与降级
def test_decode_mode_verifies_media(tmp_path):
    pytest.importorskip("PIL")
    result, _root, _entry = _run(tmp_path, [fx.make_record("s-decode")], unique_id="task_decode",
                                 overrides={"file_check_mode": "decode"})
    admitted = _lists(result)[0]
    assert len(admitted) == 1
    image_check = admitted[0]["admission"]["file_checks"]["image"]
    assert image_check["mode"] == "decode"
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert image_check["status"] == 1
    assert image_check["process_status"] == "decoded"


def test_decode_mode_detects_broken_image_above_header_check(tmp_path):
    pytest.importorskip("PIL")
    records = [fx.make_record("s-hdr-only", image="media/img_broken.png")]
    result, _root, _entry = _run(tmp_path, records, unique_id="task_hdr",
                                 overrides={"file_check_mode": "header"})
    assert _lists(result)[2][0]["reason"] == "file_signature_mismatch"

    # decode 模式下同一记录同样被原始隔离（原因更具体）
    result2, _root2, _entry2 = _run(tmp_path, records, unique_id="task_dec",
                                    overrides={"file_check_mode": "decode"})
    assert _lists(result2)[2][0]["reason"] in {"decode_failed", "file_signature_mismatch"}


def test_decode_mode_missing_decoder_fails_or_degrades(tmp_path, monkeypatch):
    record = fx.make_record("s-degrade")
    monkeypatch.setattr(admission_module, "available_backends", lambda: {
        "pillow": False, "opencv": False, "wave": True,
        "missing": ["pillow", "opencv"],
        "image_decoder": None, "video_decoder": None, "text_decoder": "utf-8",
    })
    with pytest.raises(inference.DependencyError):
        _run(tmp_path, [record], unique_id="task_deg_fail",
             overrides={"file_check_mode": "decode"})

    result, _root, _entry = _run(tmp_path, [record], unique_id="task_deg_ok",
                                 overrides={"file_check_mode": "decode",
                                            "allow_degraded": True})
    assert result["status"] == "degraded"
    admitted = _lists(result)[0]
    assert len(admitted) == 1
    assert admitted[0]["admission"]["file_checks"]["image"]["status"] == 1
    assert admitted[0]["admission"]["file_checks"]["image"]["process_status"] == "header_only"
    assert _stats(result)["degraded_mode"] is True


# ------------------------------------------------------------------ 样例数据集与 CLI
def test_sample_dataset_classification_matches_spec(tmp_path):
    out_root = tmp_path / "output"
    result = inference.run(str(SAMPLE_ENTRY), "smoke_f1", output_root=str(out_root),
                           allow_external_input=True)
    stats = _stats(result)
    assert stats["input_lines"] == 16
    assert stats["blank_lines"] == 0
    assert stats["input"] == 16
    assert stats["valid_records"] == 14
    assert stats["bad_lines"] == 2
    assert stats["admitted"] == 4
    assert stats["returned"] == 3
    assert stats["origin_quarantined"] == 3
    assert stats["sample_quarantined"] == 4
    assert stats["locked"] == 1
    assert stats["conservation_ok"] is True
    assert len(fx.read_jsonl(result["outputFile"])) == 4
    assert len(fx.read_jsonl(result["originQuarantineFile"])) == 5  # 3 条 + 2 个坏行
    assert stats["entrypoint"] == "10_admitted.jsonl"


def test_cli_runs_on_sample_dataset(tmp_path, capsys):
    code = inference.main(["--input", str(SAMPLE_ENTRY), "--unique-id", "smoke_cli",
                           "--output-root", str(tmp_path / "output_smoke"), "--is-last"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["code"] == 0
    assert payload["data"]["admitted"] == 4
    # [v4.2] 阶段返回形态统一：outputFile 始终为文件内容对象，去 lineageFile
    assert payload["data"]["dataShape"] == {"outputFile": "file-object"}


def test_cli_failure_exit_code(tmp_path, capsys):
    code = inference.main(["--input", str(tmp_path / "missing.jsonl"),
                           "--unique-id", "smoke_fail",
                           "--output-root", str(tmp_path / "output_smoke")])
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["code"] == 1


def test_cli_data_shape_describes_both_forms():
    assert inference.cli_data_shape("success", False) == {"outputFile": "file-object"}
    assert inference.cli_data_shape("success", True) == {"outputFile": "file-object"}
    assert inference.cli_data_shape("failed", True) == {}


def test_stage_file_object_contains_all_artifacts(tmp_path):
    result, _root, _entry = _run(tmp_path, [fx.make_record("s-1")])
    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == "10_admitted.jsonl"
    paths = {item["path"] for item in payload["files"]}
    assert {"10_admitted.jsonl", "10_return.jsonl", "10_origin_quarantine.jsonl",
            "10_sample_quarantine.jsonl", "manifest.json"} <= paths
    assert any(path.startswith("media/") for path in paths)
    # [v4.2] 血缘视图已停用：不再提供 build_stage_payload/build_intermediate_lineage
    assert not hasattr(inference, "build_stage_payload")
    assert not hasattr(inference, "build_intermediate_lineage")
