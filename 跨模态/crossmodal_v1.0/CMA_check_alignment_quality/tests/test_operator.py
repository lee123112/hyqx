"""端到端算子测试：任务隔离、统计守恒、台账、报告、血缘、降级与失败路径。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（显式降级/显式失败）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))

from utils import alignment_qc as qc
from utils import reverse_verify as rv
from utils.schemas import ConfigError, PredictRequest
from utils.transport import restore_file_object
from tests.conftest import StubEncoder, write_jsonl, write_png, write_wav, write_mp4

MANIFEST_NAME = "manifest.json"


def _healthy(sample_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "text": {"content": "金毛犬在草地上接飞盘"},
        "image": {"path": "media/ok_a.png", "brisque": 30.5},
        "qc": {"reverse_verify": {"passed": True, "score": 0.6,
                                  "check_status": "checked"},
               "text_fluency": {"ppl": 38.0}},
    }


def _dataset(tmp_path: Path) -> Path:
    """构造输入包：媒体清单根 = 入口 JSONL 所在目录（``input/``），媒体在 ``media/``。"""
    input_dir = tmp_path / "input"
    media = input_dir / "media"
    write_png(media / "ok_a.png")
    write_wav(media / "tone.wav")
    write_mp4(media / "clip_a.mp4")
    (media / "broken.png").write_bytes(b"not-a-real-image")
    rows = [
        _healthy("ok-0001"),
        {"sample_id": "ok-0002", "text": {"content": "红色方块"},
         "image": {"path": "media/ok_a.png", "brisque": 28.0},
         "qc": {"reverse_verify": {"passed": True, "check_status": "checked",
                                   "score": 0.55}}},
        {"sample_id": "origin-0003", "text": {"content": "缺图"},
         "image": "media/missing.png"},
        {"sample_id": "origin-0004", "text": {"content": "坏图"},
         "image": "media/broken.png"},
        {"sample_id": "sample-0005", "text": {"content": "单模态"}},
        {"sample_id": "sample-0006", "text": {"content": "  "},
         "image": "media/ok_a.png"},
        {"sample_id": "flag-0007", "text": {"content": "反向不一致"},
         "image": {"path": "media/ok_a.png"},
         "qc": {"reverse_verify": {"passed": False, "score": 0.1,
                                   "check_status": "checked"}}},
        {"sample_id": "audio-0008", "video": "media/clip_a.mp4",
         "audio": {"path": "media/tone.wav", "snr_db": 20.0}},
        # 缺少上游反向结论 → 需要本地反向检索（无编码器时为 degraded）
        {"sample_id": "pool-0009", "text": {"content": "待本地反向检索"},
         "image": {"path": "media/ok_a.png"}},
        "not-json-at-all",
        "[1,2,3]",
        "",
    ]
    return write_jsonl(input_dir / "20_candidate_pool.jsonl", rows)


def _run(dataset: Path, tmp_path: Path, unique_id: str = "task_1_file_1", **kwargs):
    return inference.run(
        input_file=str(dataset), unique_id=unique_id,
        config_path=str(inference.default_config_path()),
        output_root=str(tmp_path / "out"), allow_external_input=True, **kwargs)


# --------------------------------------------------------------------------- #
# 基本运行与产物
# --------------------------------------------------------------------------- #

def test_run_produces_all_outputs_and_conserves(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)

    run_dir = Path(result["runDir"])
    assert run_dir == tmp_path / "out" / "task_1_file_1" / qc.OPERATOR_NAME / "run001"
    for name in (qc.MAIN_NAME, qc.CHECKS_NAME, qc.REVERSE_NAME,
                 qc.ORIGINAL_QUARANTINE_NAME, qc.SAMPLE_QUARANTINE_NAME,
                 qc.FLAGGED_NAME, qc.REJECTS_NAME, qc.REPORT_NAME,
                 qc.SUPPLEMENT_NAME, "manifest.json", "run.log"):
        assert (run_dir / name).is_file(), name
    # [v4.2] 血缘/阶段归档已停用：不再落盘 lineage.json / stage_context.json
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()

    stats = result["statistics"]
    assert stats["input"] == 11                    # 12 行去掉 1 个空行
    assert stats["rejected"] == 2                  # 非法 JSON + 非对象
    assert stats["conservation_ok"] is True
    assert stats["input"] == (stats["output"] + stats["rejected"]
                             + stats["origin_quarantined"]
                             + stats["sample_quarantined"])
    assert stats["output"] == 5                    # ok-0001/0002, flag-0007, audio-0008, pool-0009
    assert stats["origin_quarantined"] == 2
    assert stats["sample_quarantined"] == 2


def test_report_is_pending_writeback_and_version_pending(media_root: Path,
                                                         tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    report = json.loads((Path(result["runDir"]) / qc.REPORT_NAME).read_text("utf-8"))
    assert report["status"] == "pending_writeback"
    assert report["writeback"]["writeback_completed"] is False
    assert report["writeback"]["writeback_by"] == "operator_24"
    assert report["writeback"]["version_state"] == "pending_release"
    assert report["statistics"]["input"] == result["statistics"]["input"]
    assert report["operator_number"] == 20
    # 同一样本可同时命中多项失败：
    #   - 单模态(sample-0005) 与 空文本(sample-0006) 各 1 条 → 有效模态不足；
    #   - 缺图(origin-0003) 与 坏图(origin-0004) 的媒体不可用，同样不足 2 个可用模态。
    assert report["checks"][qc.CHECK_MODALITIES]["failed"] == 4
    # 而「全量文件可打开」只由真正的文件问题触发，恰好 2 条
    assert report["checks"][qc.CHECK_FILE_OPENABLE]["failed"] == 2
    # 6 条样本的媒体全部可正常打开/解码（ok-0001/0002、sample-0006、flag-0007、audio-0008、pool-0009）
    assert report["checks"][qc.CHECK_FILE_OPENABLE]["ok"] == 6


def test_run_result_has_no_lineage_outputs(media_root: Path, tmp_path: Path) -> None:
    """[v4.2] 血缘/阶段归档已停用：返回值不再含 lineage 键，也不落盘血缘文件。"""
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    run_dir = Path(result["runDir"])

    for key in ("lineageFile", "stageContextFile", "lineage", "stageLineage",
                "priorLineage"):
        assert key not in result, key
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()

    report = json.loads((run_dir / qc.REPORT_NAME).read_text("utf-8"))
    assert report["report_id"] == "qreport-task_1_file_1-run001"
    assert report["status"] == "pending_writeback"


def test_output_is_utf8_with_preserved_chinese(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    raw = Path(result["outputFile"]).read_bytes()
    text = raw.decode("utf-8")                     # 严格 UTF-8，失败即抛错
    assert "金毛犬在草地上接飞盘" in text
    assert "\\u" not in text                       # 中文未被转义


def test_main_output_appends_namespace_without_losing_upstream(
        media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    first = json.loads(Path(result["outputFile"]).read_text("utf-8").splitlines()[0])
    assert first["sample_id"] == "ok-0001"
    assert first["text"]["content"] == "金毛犬在草地上接飞盘"   # 原字段保留
    node = first["processing"]["20"]
    assert node["label"] == qc.LABEL_PASSED
    assert node["human_verified"] is False
    assert node["review_required"] is False
    assert first["qc"]["text_fluency"]["ppl"] == 38.0          # 上游 qc 未被覆盖


def test_rejected_lines_are_traced_with_line_numbers(media_root: Path,
                                                     tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    rejects = [json.loads(line) for line
               in Path(result["rejectFile"]).read_text("utf-8").splitlines() if line]
    assert len(rejects) == 2
    assert {item["reason"] for item in rejects} == {"json_parse_error",
                                                   "record_not_object"}
    assert sorted(item["line"] for item in rejects) == [10, 11]
    for item in rejects:
        assert item["destination"] == qc.DESTINATION_ORIGIN


def test_quarantine_ledgers_carry_records(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    run_dir = Path(result["runDir"])
    origin = [json.loads(line) for line
              in (run_dir / qc.ORIGINAL_QUARANTINE_NAME).read_text("utf-8").splitlines() if line]
    sample = [json.loads(line) for line
              in (run_dir / qc.SAMPLE_QUARANTINE_NAME).read_text("utf-8").splitlines() if line]
    assert {item["reason"] for item in origin} == {"media_missing", "media_unreadable"}
    assert {item["reason"] for item in sample} == {"insufficient_modalities", "text_empty"}
    assert all(item["label"] == qc.LABEL_ORIGIN_UNPARSABLE
               or item["label"] == qc.LABEL_ORIGIN_BROKEN for item in origin)
    assert all(item["label"] == qc.LABEL_SAMPLE_SEMANTIC for item in sample)
    assert all("record" in item for item in sample)


def test_flagged_records_stay_in_main_and_sidecar(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    run_dir = Path(result["runDir"])
    flagged = [json.loads(line) for line
               in (run_dir / qc.FLAGGED_NAME).read_text("utf-8").splitlines() if line]
    main_ids = [json.loads(line)["sample_id"] for line
                in (run_dir / qc.MAIN_NAME).read_text("utf-8").splitlines() if line]
    assert "flag-0007" in main_ids                # 不拦样本
    assert "flag-0007" in [item["sample_id"] for item in flagged]
    # 旁路 flagged 不与主记录重复加总
    assert result["statistics"]["output"] == len(main_ids)
    assert result["statistics"]["flagged"] == len(flagged)
    assert result["statistics"]["output"] != len(main_ids) + len(flagged)


def test_supplement_list_written_for_skewed_batch(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path)
    payload = json.loads((Path(result["runDir"]) / qc.SUPPLEMENT_NAME).read_text("utf-8"))
    assert payload["batch_isolation"] is False
    assert "route_distribution" in payload
    assert set(payload["route_distribution"]) >= {"image+text", "audio+video"}


# --------------------------------------------------------------------------- #
# 降级与失败
# --------------------------------------------------------------------------- #

def test_pool_records_degrade_when_clip_unavailable(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    previous = rv.get_shared_encoder()
    rv.set_shared_encoder(None)
    try:
        result = _run(dataset, tmp_path, unique_id="task_1_file_deg")
    finally:
        rv.set_shared_encoder(previous)

    assert result["status"] == "degraded"
    assert result["statistics"]["degraded_records"] >= 1
    assert result["statistics"]["reverse_verification"]["model_unavailable"] >= 1
    flagged = [json.loads(line) for line
               in Path(result["runDir"], qc.FLAGGED_NAME).read_text("utf-8").splitlines() if line]
    assert "pool-0009" in [item["sample_id"] for item in flagged]
    reverse = [json.loads(line) for line
               in Path(result["runDir"], qc.REVERSE_NAME).read_text("utf-8").splitlines() if line]
    entry = [item for item in reverse if item["sample_id"] == "pool-0009"][0]
    assert entry["check_status"] == "insufficient_evidence"
    assert entry["reason"] == "model_unavailable"
    assert entry["similarity"] is None            # 绝不伪造分数


def test_pool_records_verified_with_shared_encoder(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    encoder = StubEncoder(dim=4, vectors={
        "text:待本地反向检索": [1.0, 0.0, 0.0, 0.0],
        "text:金毛犬在草地上接飞盘": [0.0, 1.0, 0.0, 0.0],
        "text:红色方块": [0.0, 0.0, 1.0, 0.0],
        "text:反向不一致": [0.0, 0.0, 0.0, 1.0],
        "image:ok_a.png": [1.0, 0.0, 0.0, 0.0],
    })
    previous = rv.get_shared_encoder()
    rv.set_shared_encoder(encoder)
    try:
        result = _run(dataset, tmp_path, unique_id="task_1_file_enc")
    finally:
        rv.set_shared_encoder(previous)

    reverse = [json.loads(line) for line
               in Path(result["runDir"], qc.REVERSE_NAME).read_text("utf-8").splitlines() if line]
    entry = [item for item in reverse if item["sample_id"] == "pool-0009"][0]
    assert entry["check_status"] in ("checked", "insufficient_evidence")
    assert entry["source"] == "local"
    node = json.loads(Path(result["outputFile"]).read_text("utf-8").splitlines()[4])
    assert node["sample_id"] in {"pool-0009", "audio-0008", "flag-0007", "ok-0002"}


def test_reverse_disabled_gives_success_without_model(media_root: Path,
                                                      tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    previous = rv.get_shared_encoder()
    rv.set_shared_encoder(None)
    try:
        result = _run(dataset, tmp_path, unique_id="task_1_file_off",
                      overrides={"reverse_verify": {"enabled": False}})
    finally:
        rv.set_shared_encoder(previous)
    assert result["status"] in ("success", "partial")
    assert result["statistics"]["reverse_verification"]["model_unavailable"] == 0


def test_degraded_disallowed_raises_structured_failure(media_root: Path,
                                                       tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    previous = rv.get_shared_encoder()
    rv.set_shared_encoder(None)
    try:
        with pytest.raises(inference.DependencyError):
            _run(dataset, tmp_path, unique_id="task_1_file_strict",
                 overrides={"allow_degraded": False})
    finally:
        rv.set_shared_encoder(previous)


def test_empty_input_is_success_and_flagged_empty(media_root: Path, tmp_path: Path) -> None:
    empty = write_jsonl(tmp_path / "input" / "empty.jsonl", [])
    result = _run(empty, tmp_path, unique_id="task_empty")
    assert result["status"] == "success"
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["empty_input"] is True
    assert Path(result["outputFile"]).read_text("utf-8") == ""


def test_duplicate_sample_id_goes_to_origin_quarantine(tmp_path: Path) -> None:
    write_png(tmp_path / "input" / "media" / "ok_a.png")
    rows = [_healthy("dup-1"), _healthy("dup-1")]
    dataset = write_jsonl(tmp_path / "input" / "dup.jsonl", rows)
    result = _run(dataset, tmp_path, unique_id="task_dup")
    assert result["statistics"]["origin_quarantined"] == 1
    assert result["statistics"]["duplicates"] == 1
    origin = [json.loads(line) for line
              in Path(result["runDir"], qc.ORIGINAL_QUARANTINE_NAME).read_text("utf-8").splitlines() if line]
    assert origin[0]["reason"] == "duplicate_identifier"


def test_invalid_utf8_input_is_rejected(media_root: Path, tmp_path: Path) -> None:
    path = tmp_path / "input" / "bad.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"sample_id": "x", "text": "\xff\xfe"}\n')
    with pytest.raises(inference.InputError):
        _run(path, tmp_path, unique_id="task_bad_utf8")


def test_invalid_config_is_rejected(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    with pytest.raises(ConfigError):
        _run(dataset, tmp_path, unique_id="task_cfg",
             overrides={"quality_subset_fallback": 1.5})


def test_illegal_unique_id_is_rejected(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    for bad in ("", "..", "a/b", "a\\b", "C:", "con", "trailing."):
        with pytest.raises(ValueError):
            _run(dataset, tmp_path, unique_id=bad)


# --------------------------------------------------------------------------- #
# 阶段串联与响应形态
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    first = _run(dataset, tmp_path, unique_id="task_a")
    second = _run(dataset, tmp_path, unique_id="task_b")
    assert first["runDir"] != second["runDir"]
    assert Path(first["runDir"]).parent == tmp_path / "out" / "task_a" / qc.OPERATOR_NAME
    assert Path(second["runDir"]).parent == tmp_path / "out" / "task_b" / qc.OPERATOR_NAME
    # 第二次运行不影响第一个任务的产物
    assert Path(first["outputFile"]).is_file()
    assert first["uniqueId"] == "task_a"


def test_second_node_consumes_previous_temp_output(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    first = _run(dataset, tmp_path, unique_id="task_chain")
    second = inference.run(
        input_file=first["outputFile"], unique_id="task_chain",
        config_path=str(inference.default_config_path()),
        output_root=str(tmp_path / "out"))
    assert second["runId"] == "run002"
    # [v4.2] 阶段内血缘累积已停用，第二个节点直接消费上一节点产物
    assert second["statistics"]["input"] == first["statistics"]["output"]


def test_cross_task_input_file_is_blocked(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    first = _run(dataset, tmp_path, unique_id="task_x")
    with pytest.raises(inference.InputError):
        inference.run(
            input_file=first["outputFile"], unique_id="task_y",
            config_path=str(inference.default_config_path()),
            output_root=str(tmp_path / "out"))


def test_stage_first_reads_uploaded_entrypoint(tmp_path: Path) -> None:
    task_input = tmp_path / "out" / "task_upload" / "input"
    write_png(task_input / "media" / "ok_a.png")
    write_jsonl(task_input / "20_candidate_pool.jsonl", [_healthy("up-1")])
    (task_input / "manifest.json").write_text(json.dumps({
        "format": "crossmodal-manifest-v1",
        "entrypoint": "20_candidate_pool.jsonl",
        "artifacts": [{"path": "20_candidate_pool.jsonl", "role": "main"}],
    }), encoding="utf-8")

    result = inference.run(
        input_file=None, unique_id="task_upload",
        config_path=str(inference.default_config_path()),
        output_root=str(tmp_path / "out"))
    assert result["statistics"]["input"] == 1
    assert result["status"] in ("success", "degraded")


def test_disabled_lineage_builders_return_empty(
        media_root: Path, tmp_path: Path) -> None:
    """[v4.2] 已停用的阶段形态函数保留存档，但不再构造血缘。"""
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path, unique_id="task_shape")
    assert inference.build_intermediate_lineage(result) == {}
    assert inference.build_stage_payload(result) == []


def test_stage_file_object_restores_entrypoint(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path, unique_id="task_pack")
    payload = inference.stage_file_object(result)
    restored = tmp_path / "restored"
    entry = restore_file_object(payload, restored)
    assert entry.name == qc.MAIN_NAME
    assert (restored / qc.MAIN_NAME).is_file()
    assert (restored / qc.REPORT_NAME).is_file()
    assert (restored / MANIFEST_NAME).is_file()


def test_predict_request_requires_strict_boolean() -> None:
    with pytest.raises(Exception):
        PredictRequest(uniqueId="a_f1", isLastOperator="false")
    request = PredictRequest(uniqueId="a_f1", isLastOperator=True)
    assert request.isLastOperator is True
    assert request.config == {}


def test_temp_partials_are_removed(media_root: Path, tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    result = _run(dataset, tmp_path, unique_id="task_tmp")
    leftovers = [path.name for path in Path(result["runDir"]).iterdir()
                 if path.name.endswith(".partial") or path.name.endswith(".tmp")]
    assert leftovers == []
