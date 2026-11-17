"""算子 26 业务与编排测试：抽样、路线、边界、异常、配置、统计、血缘、降级。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import inference
from conftest import (DEFAULT_CONFIG_YAML, annotation_block, build_package,
                      build_package_zip, config_variant, default_records, sample_record,
                      score_block, write_json, write_jsonl)
from utils import sampling
from utils import transport
from utils.schemas import ConfigError

ALL_ROUTES = ("image_text", "audio_text", "video_text")


@pytest.fixture()
def package(tmp_path) -> Path:
    """#23 可交付包（三路线开启；12 图文 + 4 音文 + 2 视文 + 各类异常行）。"""
    return build_package(tmp_path / "pkg", records=default_records(),
                         routes=ALL_ROUTES, isolation_ids=("cm-9912",))


def run_task(package_entry: Path, output_root: Path, config: Path,
             unique_id: str = "task_one", **kwargs):
    return inference.run(input_file=str(package_entry), unique_id=unique_id,
                         config_path=str(config), output_root=str(output_root),
                         **kwargs)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

def test_normal_run_samples_per_route_and_reports(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["sampled_records"] == 16
    assert stats["route_sampled"] == {"image_text": 10, "audio_text": 4,
                                      "video_text": 2}
    assert stats["route_available"] == {"image_text": 12, "audio_text": 4,
                                        "video_text": 2}
    assert stats["route_exhausted_records"] == 2
    assert stats["excluded_records"] == 2
    assert stats["bad_lines"] == 2                      # 损坏 JSON + 非对象
    assert stats["origin_quarantined"] == 3             # 缺标识 + 非法引用 + 重复标识
    assert stats["rejects_entries"] == 5
    assert stats["blank_lines"] == 1
    assert stats["input_records"] == 25
    assert stats["conservation_ok"] is True
    assert stats["conservation_failures"] == []
    assert stats["source_status"] == "可交付"
    assert stats["source_deliverable"] is True
    assert stats["excel_entries"] == 1
    assert stats["model"] is None and stats["rule_based"] is True


def test_excel_and_jsonl_are_written_and_consistent(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    excel = Path(result["excelFile"])
    rows_file = Path(result["outputFile"])
    assert excel.is_file() and excel.suffix == ".xlsx"
    assert rows_file.name == sampling.MAIN_NAME
    rows = [json.loads(line) for line in
            rows_file.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 16 == result["statistics"]["sampled_records"]
    assert result["statistics"]["samples_jsonl_rows"] == 16

    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(excel)
    assert book.sheetnames == ["交付样例", "样例报告"]
    sheet = book["交付样例"]
    headers = [cell.value for cell in sheet[1]]
    assert headers[:5] == ["序号", "样本ID", "模态", "描述摘要", "标注结论"]
    assert "评分结论" in headers
    assert sheet.max_row == 17
    excel_ids = {sheet.cell(row=index, column=2).value for index in range(2, 18)}
    assert excel_ids == {row["sample_id"] for row in rows}


def test_excel_report_sheet_documents_insufficient_routes(package, output_root,
                                                          config_file):
    result = run_task(package, output_root, config_file)
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(Path(result["excelFile"]))
    report = book["样例报告"]
    values = [str(report.cell(row=index, column=1).value)
              for index in range(1, report.max_row + 1)]
    assert any("路线不足" in value for value in values)
    text = "\n".join(str(report.cell(row=index, column=2).value)
                     for index in range(1, report.max_row + 1))
    assert "audio_text" in text or "音文" in text
    pairs = {str(report.cell(row=index, column=1).value):
             report.cell(row=index, column=2).value
             for index in range(2, report.max_row + 1)}
    assert pairs["来源包（容器）"] == "directory"
    assert pairs["来源包状态"] == "可交付"
    assert pairs["样例条数"] == 16
    assert pairs["表1 每路线上限"] == "10 条"


def test_report_files_and_no_html(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    payload = json.loads(Path(result["reportFile"]).read_text(encoding="utf-8"))
    assert payload["routes"]["audio_text"]["sampled"] == 4
    assert payload["routes"]["audio_text"]["insufficient"] is True
    assert payload["routes"]["image_text"]["insufficient"] is False
    assert payload["table1_max_samples_per_route"] == 10
    assert payload["sampled_records"] == 16
    assert payload["notes"]
    markdown = Path(result["reportMarkdownFile"]).read_text(encoding="utf-8")
    assert "生成交付样例" in markdown
    assert "抽样" in markdown
    run_dir = Path(result["runDir"])
    assert not list(run_dir.rglob("*.html"))
    assert all(path.suffix != ".html" for path in run_dir.iterdir())


def test_sampling_is_reproducible_with_same_seed(package, output_root, config_file):
    first = run_task(package, output_root, config_file, unique_id="rep_a")
    second = run_task(package, output_root, config_file, unique_id="rep_b")
    left = [json.loads(line)["sample_id"] for line in
            Path(first["outputFile"]).read_text(encoding="utf-8").strip().splitlines()]
    right = [json.loads(line)["sample_id"] for line in
             Path(second["outputFile"]).read_text(encoding="utf-8").strip().splitlines()]
    assert left == right and len(left) == 16
    assert first["statistics"]["sampling_fingerprint"] == \
        second["statistics"]["sampling_fingerprint"]


def test_sampling_fingerprint_changes_with_seed(package, output_root, config_file):
    seed_a = config_variant(config_file, "sampling_seed: 26\n", "seed_a.yaml")
    seed_b = config_variant(config_file, "sampling_seed: 99\n", "seed_b.yaml")
    first = run_task(package, output_root, seed_a, unique_id="seed_a")
    second = run_task(package, output_root, seed_b, unique_id="seed_b")
    assert first["statistics"]["sampling_fingerprint"] != \
        second["statistics"]["sampling_fingerprint"]


def test_rows_keep_required_excel_fields(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    rows = [json.loads(line) for line in
            Path(result["outputFile"]).read_text(encoding="utf-8").strip().splitlines()]
    row = [item for item in rows if item["sample_id"] == "cm-1001"][0]
    assert row["route"] == "image_text"                  # 上游字段原样保留
    assert row["text"]["content"].startswith("图文样例描述")
    node = row["operator_26"]
    assert node["route"] == "image_text"
    assert node["modal"] == "图文"
    assert node["description_summary"].startswith("图文样例描述")
    assert "对齐" in node["annotation_conclusion"]
    assert node["score_conclusion"].startswith("0.3")
    assert node["operator_number"] == 26
    assert node["human_verified"] is False
    assert node["review_required"] is True
    assert node["evidence"]["description_field"] == "text.content"
    assert node["evidence"]["score_source"] == "processing.12"
    assert row["processing"]["26"]["decision"] == "exported"


# --------------------------------------------------------------------------- #
# 隔离样本永不顶替
# --------------------------------------------------------------------------- #

def test_isolated_records_are_excluded_and_never_sampled(tmp_path, output_root,
                                                         config_file):
    """短路线 + 大量隔离样本：绝不用隔离区样本补齐。"""
    records = [sample_record("cm-%03d" % index, route="image_text",
                             description="可交付 %d" % index, deliverable=True)
               for index in range(1, 3)]
    records.append(sample_record("cm-9001", route="image_text",
                                 description="隔离标记样本",
                                 extra={"processing": {"21": {"isolated": True,
                                                             "status": "isolated",
                                                             "destination": "sample_isolation_library"}}}))
    records.append(sample_record("cm-9002", route="image_text", description="隔离清单样本"))
    records.append(sample_record("cm-9003", route="image_text",
                                 description="非可交付标记", deliverable=False))
    entry = build_package(tmp_path / "short", records=records, routes=("image_text",),
                          isolation_ids=("cm-9002",))
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["sampled_records"] == 2
    assert stats["route_available"]["image_text"] == 2
    assert stats["route_insufficient"] == ["image_text"]
    assert stats["excluded_records"] == 3
    assert stats["excluded_by_reason"] == {"upstream_isolated": 2,
                                           "record_not_deliverable": 1}
    rows = Path(result["outputFile"]).read_text(encoding="utf-8")
    for forbidden in ("cm-9001", "cm-9002", "cm-9003"):
        assert forbidden not in rows
    excluded = [json.loads(line) for line in
                Path(result["excludedFile"]).read_text(encoding="utf-8").strip().splitlines()]
    assert {item["sample_id"] for item in excluded} == {"cm-9001", "cm-9002", "cm-9003"}


def test_source_not_deliverable_produces_no_samples_and_degrades(tmp_path, output_root,
                                                               config_file):
    records = [sample_record("cm-%03d" % index, deliverable=False)
               for index in range(1, 6)]
    entry = build_package(tmp_path / "pending", records=records,
                          routes=("image_text",), version_state="pending_release",
                          status="待发布")
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert stats["sampled_records"] == 0
    assert stats["source_deliverable"] is False
    assert stats["sample_generation_failed"] is True
    assert stats["degrade_reason"] == "source_not_deliverable"
    assert stats["conservation_ok"] is True
    # 失败时不得编造样例，也不得从隔离区顶替
    assert Path(result["outputFile"]).read_text(encoding="utf-8") == ""
    assert Path(result["excelFile"]).is_file()
    report = json.loads(Path(result["reportFile"]).read_text(encoding="utf-8"))
    assert report["sample_generation_failed"] is True
    assert report["degrade_reason"] == "source_not_deliverable"


def test_strict_source_requires_deliverable_package(tmp_path, output_root, config_file):
    records = [sample_record("cm-001", deliverable=False)]
    entry = build_package(tmp_path / "pending2", records=records,
                          routes=("image_text",), version_state="pending_release",
                          status="待发布")
    strict = config_variant(config_file, "require_deliverable_source: true\n",
                            "strict.yaml")
    result = run_task(entry, output_root, strict)
    assert result["status"] == "failed"
    assert result["statistics"]["sampled_records"] == 0


# --------------------------------------------------------------------------- #
# 边界与异常输入
# --------------------------------------------------------------------------- #

def test_empty_file_degrades_without_fabrication(tmp_path, output_root, config_file):
    entry = write_jsonl(tmp_path / "empty" / "train" / "samples.jsonl", [])
    write_json(tmp_path / "empty" / "version.json",
               {"format": "crossmodal-delivery-version-v1", "version_state": "deliverable",
                "status": "可交付", "enabled_routes": ["image_text"]})
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["input_records"] == 0
    assert stats["empty_input"] is True
    assert stats["sampled_records"] == 0
    assert stats["conservation_ok"] is True
    assert result["status"] == "degraded"
    assert stats["degrade_reason"] == "no_deliverable_sample"


def test_all_lines_broken_fails(tmp_path, output_root, config_file):
    entry = build_package(tmp_path / "broken",
                          records=['{"a": ', 'not json at all', '["x"]'],
                          routes=("image_text",))
    result = run_task(entry, output_root, config_file)
    assert result["status"] == "failed"
    assert result["statistics"]["valid_records"] == 0
    assert result["statistics"]["bad_lines"] == 3


def test_gbk_encoding_goes_to_rejects_and_stops(tmp_path, output_root, config_file):
    base = tmp_path / "gbk"
    entry = base / "train" / "samples.jsonl"
    entry.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sample_record("cm-001"), ensure_ascii=False).encode("utf-8")
    entry.write_bytes(payload + b"\n" + "中文编码错误行\n".encode("gbk"))
    write_json(base / "version.json",
               {"format": "crossmodal-delivery-version-v1", "version_state": "deliverable",
                "status": "可交付", "enabled_routes": ["image_text"]})
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert stats["bad_lines"] == 1
    assert stats["sampled_records"] == 1
    rejects = Path(result["rejectsFile"]).read_text(encoding="utf-8")
    assert "encoding_error" in rejects


def test_missing_identifier_and_unsafe_path_and_duplicate_go_to_rejects(tmp_path,
                                                                       output_root,
                                                                       config_file):
    records = [sample_record("cm-001"),
               sample_record("cm-002", media=["C:/abs/x.png"]),
               sample_record("cm-003", media=["../up.png"]),
               sample_record("cm-001"),
               {"routes": ["image_text"], "text": {"content": "缺标识"}}]
    entry = build_package(tmp_path / "reject", records=records,
                          routes=("image_text",))
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 4
    assert stats["origin_by_reason"] == {"unsupported_media_path": 2,
                                         "missing_field:sample_id": 1,
                                         "duplicate_identifier": 1}
    assert stats["sampled_records"] == 1
    assert stats["conservation_ok"] is True


def test_records_without_description_are_sampled_but_counted(tmp_path, output_root,
                                                             config_file):
    records = [sample_record("cm-001", description="正常描述"),
               sample_record("cm-002", description=None, extra={"text": {}})]
    entry = build_package(tmp_path / "nodesc", records=records, routes=("image_text",))
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["sampled_records"] == 2
    assert stats["records_without_description"] == 1
    rows = [json.loads(line) for line in
            Path(result["outputFile"]).read_text(encoding="utf-8").strip().splitlines()]
    blank = [row for row in rows if row["sample_id"] == "cm-002"][0]
    assert blank["operator_26"]["description_summary"] in (None, "")
    assert blank["operator_26"]["evidence"]["description_field"] is None
    full = [row for row in rows if row["sample_id"] == "cm-001"][0]
    assert full["operator_26"]["description_summary"] == "正常描述"


def test_route_disabled_records_are_kept_not_rejected(tmp_path, output_root, config_file):
    records = [sample_record("cm-001", route="image_text"),
               sample_record("cm-002", route="audio_text")]
    entry = build_package(tmp_path / "one_route", records=records,
                          routes=("image_text",))
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["sampled_records"] == 1
    assert stats["route_disabled_records"] == 1
    assert stats["route_disabled_by_reason"] == {"route_not_enabled": 1}
    assert stats["conservation_ok"] is True
    skipped = [json.loads(line) for line in
               Path(result["skippedFile"]).read_text(encoding="utf-8").strip().splitlines()]
    assert any(item["reason_code"] == "route_not_enabled" for item in skipped)


def test_records_without_route_are_kept_not_rejected(tmp_path, output_root, config_file):
    records = [{"sample_id": "cm-noroute", "modalities": ["text"],
                "operator_23": {"decision": "deliverable"}}]
    entry = build_package(tmp_path / "noroute", records=records,
                          routes=("image_text",))
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["sampled_records"] == 0
    assert stats["route_disabled_records"] == 1
    assert stats["route_disabled_by_reason"] == {"no_applicable_route": 1}
    assert stats["conservation_ok"] is True
    assert result["status"] == "degraded"
    assert stats["degrade_reason"] == "no_deliverable_sample"


def test_multi_route_record_uses_primary_route_once(tmp_path, output_root, config_file):
    record = sample_record("cm-multi", route="image_text",
                           modalities=["image", "video", "text"])
    record["routes"] = ["image_text", "video_text"]
    entry = build_package(tmp_path / "multi", records=[record],
                          routes=ALL_ROUTES)
    result = run_task(entry, output_root, config_file)
    stats = result["statistics"]
    assert stats["sampled_records"] == 1
    assert stats["route_sampled"] == {"image_text": 1, "audio_text": 0,
                                      "video_text": 0}
    assert stats["multi_route_records"] == 1
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("extra,needle", [
    ("unknown_key: 1\n", "unknown_key"),
    ("max_samples_per_route: 11\n", "max_samples_per_route"),
    ("max_samples_per_route: 0\n", "max_samples_per_route"),
    ("max_samples_per_route: -3\n", "max_samples_per_route"),
    ("format: csv\n", "format"),
    ("format: html\n", "format"),
    ("source_status: 待发布\n", "source_status"),
    ("description_summary_chars: 0\n", "description_summary_chars"),
    ("sampling_mode: shuffle\n", "sampling_mode"),
    ("sampling_seed: -1\n", "sampling_seed"),
    ("routes:\n  local_annotation: true\n", "routes"),
    ("stage_id: ''\n", "stage_id"),
    ("transport:\n  format: legacy-v0\n", "transport"),
])
def test_invalid_config_is_rejected(tmp_path, config_file, extra, needle):
    path = config_variant(config_file, extra)
    with pytest.raises((ConfigError, inference.InputError)) as excinfo:
        inference.load_config(str(path))
    assert needle in str(excinfo.value)


def test_missing_config_file_is_rejected(tmp_path):
    with pytest.raises(inference.InputError):
        inference.load_config(str(tmp_path / "nope.yaml"))


def test_broken_yaml_is_rejected(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("operator: [unclosed\n", encoding="utf-8")
    with pytest.raises(inference.InputError):
        inference.load_config(str(path))


def test_plan_settings_style_config_is_accepted(tmp_path):
    """计划文档里的 operator.settings（key=value 字符串数组）也可直接使用。"""
    path = tmp_path / "plan.yaml"
    path.write_text(
        "operator:\n"
        "  number: 26\n"
        "  id: delivery_sample\n"
        "  name: 生成交付样例\n"
        "  label: 生成交付样例\n"
        "  version: v1.0.0\n"
        "  enabled: true\n"
        "  settings:\n"
        '    - "format=xlsx"\n'
        '    - "source_status=可交付。"\n'
        '    - "max_samples_per_route=7"\n'
        '    - "enabled=true"\n'
        "stage_id: S8\n"
        "transport:\n"
        "  format: crossmodal-files-v1\n"
        "  max_response_bytes: 67108864\n", encoding="utf-8")
    config = inference.load_config(str(path))
    assert config.max_samples_per_route == 7          # settings 被提升为顶层配置
    assert config.source_status == "可交付"           # 结尾句号被清理
    assert config.format == "xlsx"


def test_enabled_false_is_allowed_and_degrades(package, output_root, tmp_path,
                                               config_file):
    path = config_variant(config_file, "enabled: false\n", "disabled.yaml")
    result = run_task(package, output_root, path)
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert stats["enabled"] is False
    assert stats["sampled_records"] == 0
    assert stats["degrade_reason"] == "disabled_by_config"
    assert stats["conservation_ok"] is True


def test_override_is_whitelisted(package, output_root, config_file):
    result = run_task(package, output_root, config_file,
                      overrides={"max_samples_per_route": 3})
    assert result["statistics"]["sampled_records"] == 3 + 3 + 2
    with pytest.raises(ConfigError):
        run_task(package, output_root, config_file,
                 overrides={"format": "csv"})
    with pytest.raises(ConfigError):
        run_task(package, output_root, config_file,
                 overrides={"max_samples_per_route": 100})


# --------------------------------------------------------------------------- #
# 路径与任务隔离
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "   ", "../escape", "a/b", "C:/abs", ".", "..",
                                 "con", "x."])
def test_invalid_unique_id_is_rejected(package, output_root, config_file, bad):
    with pytest.raises(ValueError):
        run_task(package, output_root, config_file, unique_id=bad)


def test_two_tasks_are_isolated(package, tmp_path, config_file, output_root):
    first = run_task(package, output_root, config_file, unique_id="iso_one")
    second = run_task(package, output_root, config_file, unique_id="iso_two")
    assert first["runDir"] != second["runDir"]
    assert Path(first["runDir"]).is_relative_to(output_root / "iso_one")
    assert Path(second["runDir"]).is_relative_to(output_root / "iso_two")
    assert (output_root / "iso_one" / "input").is_dir() is False
    # [v4.2] 血缘输出已停用：不再写 lineage.json，改用运行目录与结果口径校验隔离
    assert Path(first["runDir"]).name == "run001"
    assert not (Path(first["runDir"]) / "lineage.json").exists()


def test_repeated_run_uses_new_run_directory(package, output_root, config_file):
    first = run_task(package, output_root, config_file, unique_id="rerun_f1")
    second = run_task(package, output_root, config_file, unique_id="rerun_f1")
    assert Path(first["runDir"]).name == "run001"
    assert Path(second["runDir"]).name == "run002"
    assert Path(first["runDir"]).is_dir() and Path(second["runDir"]).is_dir()


def test_input_outside_task_rejected_when_restricted(package, output_root,
                                                     config_file):
    with pytest.raises(inference.InputError):
        run_task(package, output_root, config_file,
                 allow_external_input=False)


def test_missing_input_file_is_rejected(tmp_path, output_root, config_file):
    with pytest.raises(inference.InputError):
        run_task(tmp_path / "nope.jsonl", output_root, config_file)


def test_directory_without_jsonl_is_rejected(tmp_path, output_root, config_file):
    empty = tmp_path / "emptydir"
    empty.mkdir()
    with pytest.raises(inference.InputError):
        run_task(empty, output_root, config_file)


# --------------------------------------------------------------------------- #
# 产物与传输形态（[v4.2] 血缘输出已停用）
# --------------------------------------------------------------------------- #

def test_run_has_no_lineage_and_real_statistics(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    # [v4.2] 血缘/阶段上下文停用：run() 不再返回 lineage 相关键
    for key in ("lineage", "lineageFile", "stageLineage", "stageContext",
                "contextFile", "node"):
        assert key not in result, key
    assert not (Path(result["runDir"]) / "lineage.json").exists()
    assert not (Path(result["runDir"]) / "stage_context.json").exists()
    assert result["statistics"]["sampled_records"] == 16
    assert result["statistics"]["conservation_ok"] is True
    assert result["report"]["max_samples_per_route"] == 10
    assert result["report"]["sampling_seed"] == 26
    assert result["report"]["status"] == "success"


def test_intermediate_and_final_shapes(package, output_root, config_file):
    """[v4.2] 两种形态都只有 outputFile 文件对象（无阶段血缘）。"""
    result = run_task(package, output_root, config_file)
    payload = inference.build_file_object(result)
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == sampling.MAIN_NAME
    assert "lineage" not in payload
    assert result["fileObject"] is payload


def test_artifact_manifest_lists_all_products(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    manifest = json.loads(Path(result["manifestPath"]).read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    paths = {item["path"] for item in manifest["artifacts"]}
    for expected in (sampling.MAIN_NAME, sampling.EXCEL_NAME, sampling.SKIPPED_NAME,
                     sampling.EXCLUDED_NAME, sampling.REJECTS_NAME,
                     sampling.REPORT_NAME, sampling.REPORT_MD_NAME):
        assert expected in paths, expected


def test_log_file_has_time_and_counts(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    lines = Path(result["logFile"]).read_text(encoding="utf-8").strip().splitlines()
    assert lines
    assert all("|" in line for line in lines)
    assert any("sampled" in line for line in lines)
    text = "\n".join(lines)
    assert "一只橘猫" not in text


def test_products_are_utf8(package, output_root, config_file):
    result = run_task(package, output_root, config_file)
    for key in ("outputFile", "skippedFile", "excludedFile", "rejectsFile",
                "reportFile", "reportMarkdownFile"):
        Path(result[key]).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 仅从可交付包取样：输入形态
# --------------------------------------------------------------------------- #

def test_zip_package_input_works(tmp_path, output_root, config_file):
    zip_path = build_package_zip(tmp_path, "pkg.zip", records=default_records(),
                                 routes=ALL_ROUTES, isolation_ids=("cm-9912",))
    result = inference.run(input_file=str(zip_path), unique_id="zip_task",
                           config_path=str(config_file),
                           output_root=str(output_root))
    assert result["status"] == "success"
    assert result["statistics"]["sampled_records"] == 16
    assert result["statistics"]["package_container"] == "zip"


def test_zip_slip_package_is_rejected(tmp_path, output_root, config_file):
    import zipfile

    bad = tmp_path / "slip.zip"
    with zipfile.ZipFile(bad, "w") as bundle:
        bundle.writestr("../escape.jsonl", "{}")
        bundle.writestr("train/samples.jsonl", "{}\n")
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(bad), unique_id="slip_task",
                      config_path=str(config_file), output_root=str(output_root))


def test_isolated_directory_is_never_read(tmp_path, output_root, config_file):
    """隔离区目录里的样本绝不参与抽样。"""
    pkg = build_package(tmp_path / "pkg_iso", records=[
        sample_record("cm-001", description="正常"),
        sample_record("cm-002", description="正常2")], routes=("image_text",))
    quarantined = tmp_path / "quarantine"
    write_jsonl(quarantined / "sample_quarantine.jsonl",
                [sample_record("cm-9001", description="隔离区样本")] * 5)
    result = run_task(pkg, output_root, config_file)
    assert result["statistics"]["sampled_records"] == 2
    text = Path(result["outputFile"]).read_text(encoding="utf-8")
    assert "cm-9001" not in text


# --------------------------------------------------------------------------- #
# 样例数据集（仓库自带，真实可跑）
# --------------------------------------------------------------------------- #

def test_repo_sample_dataset_is_consistent(tmp_path, output_root, config_file):
    sample = Path(inference.operator_dir()) / "dataset" / "sample" / "demo.jsonl"
    assert sample.is_file()
    result = inference.run(input_file=str(sample), unique_id="sample_task",
                           config_path=str(config_file),
                           output_root=str(output_root))
    stats = result["statistics"]
    assert stats["sampled_records"] == result["statistics"]["samples_jsonl_rows"]
    assert stats["conservation_ok"] is True
    assert stats["source_deliverable"] is True
    assert result["status"] == "success"
