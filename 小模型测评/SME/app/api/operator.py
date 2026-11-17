from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import time
import zipfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Query, UploadFile
from fastapi.responses import FileResponse

from ..core.config import settings
from ..core.envelope import ApiError, ok
from ..core.export_paths import BundleExporter
from ..core.ids import is_valid_id, now
from ..core.lineage import LineageWriter, sha256_file
from ..core.logging import RunLogger
from ..core.registry import resolve
from ..core.security import logical_path, safe_join, sanitize_filename
from ..datasets.parser import parse_dataset
from ..engines import service as engine_service
from ..eval import rules, templates, task_factory
from ..store import db

router = APIRouter(tags=["operator"])


def _row_counts(samples: list[dict[str, Any]], result: dict[str, Any]) -> tuple[int, int]:
    if result.get('summary', {}).get('scope') == 'dataset':
        total = int(result['summary']['total'])
        return total, total
    return len(samples), len(result.get('samples') or [])


def _workspace(unique_id: str) -> Path:
    return safe_join(settings.workspace_dir, unique_id)


def _resolve_input(unique_id: str, raw: str | None) -> Path:
    root = _workspace(unique_id)
    if raw:
        value = raw
        if value.startswith("tmp://"):
            value = value[len("tmp://"):]
            if "/" in value:
                prefix, rest = value.split("/", 1)
                value = rest if prefix == unique_id else value
        if value.startswith("/workspace/"):
            value = value[len("/workspace/"):]
            if "/" in value:
                prefix, rest = value.split("/", 1)
                value = rest if prefix == unique_id else value
        target = safe_join(root, value, must_exist=True)
        if target.is_dir():
            raise ApiError(422, "INVALID_INPUT", "inputFile 必须是文件")
        return target
    input_dir = root / "input"
    if not input_dir.exists():
        raise ApiError(422, "INVALID_INPUT", "请先调用 uploadFile 上传数据集")
    files = [item for item in input_dir.iterdir() if item.is_file()]
    if not files:
        raise ApiError(422, "INVALID_INPUT", "输入目录为空")
    return sorted(files)[0]


def _complete_measurements(type_: str, samples: list[dict[str, Any]]) -> bool:
    keys = [metric["key"] for metric in templates.TEMPLATES[type_]["metrics"]]
    for sample in samples:
        measurements = sample.get("measurements") or {}
        for model in ("baseline", "candidate"):
            values = measurements.get(model) or {}
            if any(not rules.is_num(values.get(key)) for key in keys):
                return False
    return bool(samples)


def _merge_engine(type_: str, samples: list[dict[str, Any]], config: dict[str, Any],
                  seed: int, strict: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = engine_service.measure_samples(type_, samples, config, seed=seed, strict=strict)
    merged = engine_service.apply_measurements(samples, result)
    missing = engine_service.missing_metrics(type_, result)
    if missing and strict:
        raise ApiError(422, "MEASUREMENTS_INCOMPLETE", "测量引擎缺少指标", missing)
    return merged, {
        "provenance": result.provenance, "meta": result.meta,
        "missing": missing, "notes": result.notes,
    }


def _feedback_rows(task_id: str, type_: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    failed = [sample for sample in result["samples"] if not sample["passed"]]
    for index, sample in enumerate(failed, start=1):
        rows.append({
            "id": f"{task_id}-F{index}",
            "taskId": task_id,
            "sampleId": sample["id"],
            "issue": "、".join(sample.get("issues") or []) or f"{sample.get('grade')} 级样本需按分级策略应用",
            "sourceStep": sample.get("sourceStep") or "数据处理",
            "suggestion": rules.suggestion(type_, sample),
            "parameters": sample.get("parameters") or {},
            "status": "pending",
            "note": "",
            "createdAt": now(),
        })
    return rows


def _report_markdown(title: str, task_id: str, type_: str, mode: str,
                     result: dict[str, Any], config: dict[str, Any],
                     feedback: list[dict[str, Any]],
                     engine_meta: dict[str, Any] | None = None) -> str:
    if 'trainingProtocol' in result:
        from ..experiments.training import protocol_markdown
        return protocol_markdown(result, title=title)
    summary = result["summary"]
    conclusion = ("评测达标" if summary["accepted"] and summary["issues"] == 0 else
                  (f"批次指标达标 · {summary['issues']} 条待优化" if summary["accepted"]
                   else "待优化"))
    lines = [
        "# 数据集评测报告", "",
        f"- 任务：{title}（{task_id}）",
        f"- 类型：{templates.TEMPLATES[type_]['name']}",
        f"- 模式：{'Demo 模拟' if mode == 'demo' else '实测'}",
        f"- 结论：{conclusion}",
        f"- 样本：{summary['total']} 条；达标 {summary['passed']} 条；问题 {summary['issues']} 条",
        "", "## 指标对比", "",
        "| 指标 | 基线 | 待评估 | 变化 | 阈值 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for metric in summary["metrics"]:
        threshold = f"分差 ≥{metric['threshold']}" if type_ == "benchmark" else metric["threshold"]
        lines.append(
            f"| {metric['name']} | {metric['baseline']}{metric['unit']} | "
            f"{metric['candidate']}{metric['unit']} | {metric['delta']}{metric['unit']} | "
            f"{threshold}{metric['unit']} | {'达标' if metric['passed'] else '未达标'} |"
        )
    if type_ == "video":
        lines += ["", f"等级分布：{json.dumps(summary['grades'], ensure_ascii=False)}"]
    lines += ["", "## 逐条结果", "", "| 样本 | 标题 | 结果 | 问题 |", "|---|---|---|---|"]
    for sample in result["samples"]:
        outcome = f"{sample['grade']} 级" if sample.get("grade") else ("达标" if sample["passed"] else "未达标")
        lines.append(f"| {sample['id']} | {sample.get('title') or sample.get('scene') or '—'} | "
                     f"{outcome} | {'、'.join(sample.get('issues') or []) or '—'} |")
    if feedback:
        lines += ["", "## 优化建议", ""]
        for item in feedback:
            lines.append(f"- **{item['sampleId']}**：{item['suggestion']}")
    engine_meta = engine_meta or {}
    meta = engine_meta.get("meta") if isinstance(engine_meta.get("meta"), dict) else {}
    provenance = engine_meta.get("provenance") if isinstance(
        engine_meta.get("provenance"), dict) else {}
    if meta or provenance:
        lines += ["", "## 测量协议", ""]
        for key, label in (
            ("engineVersion", "引擎版本"),
            ("engine", "引擎"),
            ("backend", "后端"),
            ("metricNature", "指标性质"),
            ("seed", "随机种子"),
            ("sampleCount", "样本数"),
            ("asr", "ASR"),
            ("labels", "标签"),
        ):
            value = meta.get(key)
            if value is not None:
                lines.append(f"- {label}：`{json.dumps(value, ensure_ascii=False)}`")
        if provenance:
            lines.append("- 指标来源：" + "；".join(
                f"`{key}`={value}" for key, value in sorted(provenance.items())))
        for note in engine_meta.get("notes") or []:
            lines.append(f"- 说明：{note}")
    if type_ == "benchmark":
        from ..engines.benchmark_analysis import report_lines
        lines += report_lines(meta)
    lines += ["", "## 说明", "", "本报告由小模型测评服务生成；Demo 模式为流程演示，实测模式指标需具备对应资产/标注。"]
    return "\n".join(lines) + "\n"


def _write_outputs(unique_id: str, operator_id: str, type_: str, mode: str,
                   title: str, samples: list[dict[str, Any]], result: dict[str, Any],
                   config: dict[str, Any], engine_meta: dict[str, Any],
                   logger: RunLogger) -> dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex
    out_dir = _workspace(unique_id) / operator_id / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    exporter = BundleExporter(out_dir, unique_id, type_, training_protocol='trainingProtocol' in result)
    exported = exporter.convert({'samples':samples,'result':result,'config':config,'engine':engine_meta})
    exported_samples, exported_result = exported['samples'], exported['result']
    exported_config, exported_engine = exported['config'], exported['engine']
    rows_in, rows_out = _row_counts(samples, result)
    feedback = _feedback_rows(unique_id, type_, result)

    measurements_path = out_dir / "measurements.jsonl"
    with measurements_path.open("w", encoding="utf-8") as handle:
        for sample in exported_samples:
            handle.write(json.dumps({
                "id": sample["id"], "title": sample.get("title"),
                "scene": sample.get("scene"), "sourceStep": sample.get("sourceStep"),
                "parameters": sample.get("parameters") or {},
                "highValue": bool(sample.get("highValue")),
                "measurements": sample.get("measurements"),
            }, ensure_ascii=False, separators=(",", ":")) + "\n")

    result_path = out_dir / "result.json"
    result_path.write_text(json.dumps({
        "uniqueId": unique_id, "operatorId": operator_id, "type": type_,
        "mode": mode, "title": exporter.text(title,'title'), "config": exported_config, "result": exported_result,
        "rowsIn": rows_in, "rowsOut": rows_out,
        "provenance": exported_engine.get("provenance") or {},
        "engine": exported_engine.get("meta") or {},
        "feedback": exporter.convert(feedback), "generatedAt": now(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if 'trainingProtocol' in result:
        (out_dir / 'protocol.json').write_text(
            json.dumps(exported_result['trainingProtocol'], ensure_ascii=False, indent=2), encoding='utf-8')

    report = _report_markdown(exporter.text(title,'title'), unique_id, type_, mode, exported_result,
                              exported_config, exporter.convert(feedback), exported_engine)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "summary.md").write_text(report, encoding="utf-8")

    with (out_dir / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["key", "name", "unit", "baseline", "candidate", "delta", "threshold", "passed"])
        for metric in result["summary"]["metrics"]:
            writer.writerow([metric["key"], metric["name"], metric["unit"], metric["baseline"],
                             metric["candidate"], metric["delta"], metric["threshold"], metric["passed"]])

    logger.log("输出", f"写入指标 {len(samples)} 条、报告与指标表")
    (out_dir / "run.log").write_text(
        "".join(f"[{entry['time']}] [{entry['tag']}] {entry['message']}\n"
                for entry in logger.entries),
        encoding="utf-8",
    )
    return {
        "runId": run_id, "outDir": out_dir,
        "report": out_dir / "report.md", "result": result_path,
        "measurements": measurements_path, "feedback": feedback, "exporter": exporter,
        "rowsIn": rows_in, "rowsOut": rows_out,
    }


def _finalize_artifacts(out_dir: Path, unique_id: str, operator_id: str,
                        type_: str, mode: str, result: dict[str, Any],
                        engine_meta: dict[str, Any], exporter: BundleExporter | None = None,
                        rows_in: int | None = None, rows_out: int | None = None) -> Path:
    exporter = exporter or BundleExporter(out_dir, unique_id, type_, training_protocol='trainingProtocol' in result)
    exporter.finish()
    if rows_in is None or rows_out is None:
        rows_in, rows_out = _row_counts(result.get('samples') or [], result)
    manifest = {
        "uniqueId": unique_id, "operatorId": operator_id, "type": type_, "mode": mode,
        "runId": out_dir.name, "generatedAt": now(),
        "summary": exporter.convert(result["summary"]),
        "rowsIn": rows_in, "rowsOut": rows_out,
        "provenance": exporter.convert(engine_meta.get("provenance") or {}),
        "files": [],
    }
    for item in sorted(out_dir.rglob('*')):
        if item.is_file() and item not in (out_dir/"manifest.json", out_dir/"output.zip"):
            manifest["files"].append({
                "name": item.relative_to(out_dir).as_posix(), "size": item.stat().st_size, "sha256": sha256_file(item),
            })
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    zip_path = out_dir / "output.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(out_dir.rglob('*')):
            if item.is_file() and item != out_dir/"output.zip":
                archive.write(item, arcname=item.relative_to(out_dir).as_posix())
    return zip_path


@router.post("/uploadFile")
async def upload_file(unique_id: str = Query(..., alias="uniqueId"),
                      file: UploadFile = File(...)) -> dict:
    if not is_valid_id(unique_id):
        raise ApiError(400, "INVALID_UNIQUE_ID", "uniqueId 不合法")
    target_dir = _workspace(unique_id) / "input"
    target_dir.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(file.filename or "dataset.zip")
    target = safe_join(target_dir, name)
    size = 0
    with target.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.max_upload_mb * 1024 * 1024:
                handle.close()
                target.unlink(missing_ok=True)
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "上传文件超过大小限制")
            handle.write(chunk)
    return ok({
        "inputFile": logical_path(target, unique_id),
        "fileName": name, "size": size,
        "sha256": sha256_file(target),
    })


@router.post("/predict")
def predict(body: dict[str, Any] = Body(...)) -> dict:
    started = time.monotonic()
    unique_id = str(body.get("uniqueId") or "")
    operator_id = str(body.get("operatorId") or "")
    if not is_valid_id(unique_id):
        raise ApiError(400, "INVALID_UNIQUE_ID", "uniqueId 不合法")
    found = resolve(operator_id)
    if not found:
        raise ApiError(422, "UNKNOWN_OPERATOR", "未知算子")
    operator_id, operator_meta = found
    type_ = operator_meta.get("type")
    if type_ not in templates.TEMPLATES:
        raise ApiError(422, "UNKNOWN_OPERATOR", "算子类型未注册")
    params = body.get("params") or {}
    if params.get("type") and params["type"] != type_:
        raise ApiError(422, "OPERATOR_TYPE_MISMATCH", "params.type 与 operatorId 不匹配")
    mode = str(params.get("mode") or "demo")
    if mode not in ("demo", "measured"):
        raise ApiError(422, "CONFIG_INVALID", "mode 必须是 demo 或 measured")
    config = params.get("config") or templates.default_config(type_)
    try:
        rules.validate_config(type_, config)
    except rules.RuleError as exc:
        raise ApiError(422, exc.error_code, str(exc)) from exc
    seed = engine_service.validate_seed(params.get("seed", 42))
    input_path = _resolve_input(unique_id, body.get("inputFile"))
    protocol = None
    protocol_base = input_path.parent
    if params.get('protocol') is not None:
        if mode != 'measured':
            raise ApiError(422, 'CONFIG_INVALID', '训练协议必须显式使用 measured 模式')
        protocol, protocol_base = _protocol_input(input_path, params['protocol'])
        nested_seeds = protocol.get('seeds')
        nested_seed = (nested_seeds[0] if isinstance(nested_seeds, list) and nested_seeds
                       else protocol.get('seed', seed))
        nested_seed = engine_service.validate_seed(nested_seed)
        if 'seed' in params and nested_seed != seed:
            raise ApiError(422, 'CONFIG_INVALID', 'params.seed 与协议首个种子不一致')
        seed = nested_seed
        if 'seed' in params and not any(k in protocol for k in ('seed','seeds')):
            protocol['seeds' if type_ == 'audio' else 'seed'] = [seed] if type_ == 'audio' else seed
        parsed = {'samples': [], 'name': input_path.stem}
    else:
        parsed = parse_dataset(input_path, unique_id, input_path.parent)
    samples = parsed["samples"]
    logger = RunLogger(None, unique_id)
    logger.log("开始", f"算子 {operator_id} 启动，输入 {len(samples)} 条")
    logger.log("输入", f"数据集 {parsed['name']}，模式 {mode}")

    engine_meta: dict[str, Any] = {"provenance": {}, "meta": {}, "missing": [], "notes": []}
    try:
        if protocol is not None:
            from ..experiments import training
            record = training.run_training_protocol(type_, protocol, base_dir=protocol_base)
            result = training.training_evaluation(type_, record)
            engine_meta = result['engine']
        elif mode == "demo":
            samples = rules.demoSamples(type_, samples)
            logger.log("处理", "Demo 模式：使用固定模拟指标")
        else:
            if type_ == "benchmark":
                samples, engine_meta = _merge_engine(
                    type_, samples, config, seed, strict=True)
            elif not _complete_measurements(type_, samples):
                source = str(params.get("measurementSource") or "engine")
                if source == "import":
                    raise ApiError(422, "MEASUREMENTS_REQUIRED", "实测模式缺少逐条指标")
                samples, engine_meta = _merge_engine(
                    type_, samples, config, seed, strict=True)
                logger.log("处理", "测量引擎完成逐条指标")
            else:
                engine_meta = engine_service.imported_audit(type_, samples, seed=seed)
            samples = rules.normalize_samples(samples, type_, "measured")
        if protocol is None:
            from ..experiments.training import evaluate_measured
            result = evaluate_measured(type_, samples, config, engine_meta)
        if engine_meta.get("provenance") or engine_meta.get("meta"):
            result["engine"] = dict(engine_meta)
        if protocol is not None:
            logger.log('完成', f"训练协议完成，固定 E {result['summary']['total']} 条；业务判定待定")
        else:
            logger.log("完成", f"评测完成，达标 {result['summary']['passed']} / {result['summary']['total']}")

        title = params.get("taskName") or parsed["name"]
        outputs = _write_outputs(unique_id, operator_id, type_, mode, title, samples, result,
                                 config, engine_meta, logger)
        lineage_path = outputs["outDir"] / f"lineage_{unique_id}.jsonl"
        lineage = LineageWriter(lineage_path, unique_id)
        lineage.input_file(input_path)
        lineage.record("operator", operatorId=operator_id, type=type_, mode=mode,
                       params={k: v for k, v in params.items() if k != "config"},
                       engine=engine_meta.get("meta") or {})
        for name in ("result.json", "measurements.jsonl", "report.md", "metrics.csv", "manifest.json", "protocol.json"):
            item = outputs["outDir"] / name
            if item.exists():
                lineage.output(item, kind=name)
        lineage.verdict(outcome=('undetermined' if result['summary']['accepted'] is None else
                                "accepted" if result["summary"]["accepted"] else "needs_optimization"),
                        accepted=result["summary"]["accepted"],
                        metrics={m["key"]: m["candidate"] for m in result["summary"]["metrics"]})
        incoming_lineage = body.get("lineageFile")
        if isinstance(incoming_lineage, str):
            try:
                incoming_lineage = json.loads(incoming_lineage)
            except ValueError:
                incoming_lineage = None
        if isinstance(incoming_lineage, list):
            prefix = [
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                for record in incoming_lineage[:2000] if isinstance(record, dict)
            ]
            if prefix:
                current = lineage_path.read_text(encoding="utf-8").splitlines()
                lineage_path.write_text(
                    "\n".join(prefix + current) + "\n", encoding="utf-8")
        lineage.finalize()
        logger.log("输出", f"产物目录 {logical_path(outputs['outDir'], unique_id)}")

        task_id = None
        if params.get("registerTask"):
            task_id = _register_task(unique_id, title, type_, mode, samples, result,
                                     config, outputs["feedback"], seed=seed,
                                     protocol=protocol, protocol_base=protocol_base)
            logger.log("输出", f"已登记评测任务 {task_id}")

        # Refresh run.log with every log line, then freeze the artifact manifest/zip.
        (outputs["outDir"] / "run.log").write_text(
            "".join(f"[{entry['time']}] [{entry['tag']}] {entry['message']}\n"
                    for entry in logger.entries),
            encoding="utf-8",
        )
        zip_path = _finalize_artifacts(outputs["outDir"], unique_id, operator_id,
                                       type_, mode, result, engine_meta, outputs['exporter'],
                                       outputs['rowsIn'], outputs['rowsOut'])
        lineage_records = [
            json.loads(line) for line in lineage_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        output_path = logical_path(zip_path, unique_id)
        is_last = bool(body.get("isLastOperator"))
        output_file = (base64.b64encode(zip_path.read_bytes()).decode("ascii")
                       if is_last else output_path)

        return ok({
            "jobId": f"{unique_id}:{operator_id}",
            "status": "succeeded",
            "taskId": task_id,
            "outputFile": output_file,
            "outputPath": output_path,
            "outputIsBase64": is_last,
            "lineageFile": lineage_records,
            "lineagePath": logical_path(lineage_path, unique_id),
            "terminal": False,
            "stopChain": False,
            "downstream": True,
            "gate_status": "PASS",
            "quality_status": "PENDING",
            "rowsIn": outputs['rowsIn'],
            "rowsOut": outputs['rowsOut'],
            "report": outputs['exporter'].convert(result["summary"]),
            "result": {"summary": outputs['exporter'].convert(result["summary"])},
            "logs": outputs['exporter'].convert(logger.entries),
            "missingMetrics": outputs['exporter'].convert(engine_meta.get("missing") or []),
            "traceId": body.get("traceId") or unique_id,
            "attempt": int(body.get("attempt") or 1),
            "runMode": body.get("runMode") or "gate_and_score",
            "durationMs": int((time.monotonic() - started) * 1000),
        })
    except ApiError as exc:
        logger.log("错误", exc.message)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.log("错误", str(exc)[:160])
        raise ApiError(500, "OPERATOR_FAILED", "算子执行失败") from exc


def _register_task(task_id: str, title: str, type_: str, mode: str,
                   samples: list[dict[str, Any]], result: dict[str, Any],
                   config: dict[str, Any], feedback: list[dict[str, Any]], *, seed: int = 42,
                   protocol: dict[str, Any] | None = None, protocol_base: Path | None = None) -> str:
    if protocol is not None:
        from ..eval.protocol_tasks import build_protocol_task
        from ..experiments.training import build_training_spec
        from dataclasses import asdict
        # Store resolved paths, so task reruns do not depend on operator cwd.
        resolved = asdict(build_training_spec(type_, protocol, base_dir=protocol_base))
        resolved = {k: str(v) if isinstance(v, Path) else list(v) if isinstance(v, tuple) else v
                    for k, v in resolved.items()}
        task = build_protocol_task({'id': task_id, 'name': title, 'type': type_,
            'mode': mode, 'protocol': resolved, 'config': config, 'seed': seed}, allow_existing=True)
        task.update(status='completed', completedAt=now(), result=result,
                    step=len(templates.TEMPLATES[type_]['steps']))
        db.save_task(task)
        db.replace_feedback(task_id, [])
        db.append_log(task_id, '完成', '训练协议运行完成，已保留完整数据集级结果')
        return task_id
    benchmark_meta = ((result.get("engine") or {}).get("meta") or {}) if type_ == "benchmark" else {}
    audit_meta = (result.get("engine") or {}).get("meta") or {}
    provenance = (result.get("engine") or {}).get("provenance") or {}
    measurement_source = ("demo" if mode == "demo" else
                          "engine" if audit_meta.get("evaluation") or
                          any(value != "imported" and not str(value).startswith("import") for value in provenance.values())
                          else "import")
    seed_override = {"seed": engine_service.validate_seed(benchmark_meta.get("seed", seed)),
                     "measurementSource": measurement_source}
    existing = db.get_task(task_id)
    if existing:
        if existing.get('status') == 'running':
            raise ApiError(409, 'JOB_RUNNING', '任务正在运行，不能替换登记结果')
        if (existing.get('protocol') is not None or existing.get('type') != type_
                or existing.get('mode') != mode):
            # Rebuild through the normal factory rather than keeping source,
            # mode, plan or model labels belonging to a previous protocol.
            existing = task_factory.build_task({
                'id': task_id, 'name': title, 'type': type_, 'mode': mode,
                'source': {'kind': 'upload'}, 'samples': samples, 'config': config,
                'createdAt': existing.get('createdAt'), **seed_override,
            }, allow_existing=True)
        existing.update({
            'name': title, 'type': type_, 'mode': mode, '_needsEngine': False,
            "samples": samples, "result": result, "status": "completed",
            "step": len(templates.TEMPLATES[type_]["steps"]),
            "completedAt": now(), "error": "", "config": config,
            **seed_override,
        })
        db.save_task(existing)
        for index, sample in enumerate(samples):
            db.append_log(task_id, "处理", f"样本 {sample.get('id')} 已完成")
            if index >= 50:
                break
    else:
        task = task_factory.build_task({
            "id": task_id, "name": title, "type": type_, "mode": mode,
            "source": {"kind": "upload"}, "samples": samples, "config": config,
            "autoRun": False,
            **seed_override,
        })
        task.update({
            "status": "completed", "step": len(templates.TEMPLATES[type_]["steps"]),
            "completedAt": now(), "result": result, "logs": [], "samples": samples,
            "_needsEngine": False,
        })
        db.save_task(task)
        db.append_log(task_id, "完成", "算子运行完成并登记任务")
    db.replace_feedback(task_id, feedback)
    return task_id


def _protocol_input(path: Path, request: Any) -> tuple[dict[str, Any], Path]:
    """Read an explicit protocol object or protocol.json in an uploaded ZIP."""
    if isinstance(request, dict) and request:
        return dict(request), path.parent
    if request is not True:
        raise ApiError(422, 'CONFIG_INVALID', 'protocol 必须为非空对象或 true（读取上传配置）')
    if path.suffix.lower() == '.zip':
        from ..datasets.parser import _extract_zip
        files = _extract_zip(path, path.parent / 'protocol_assets')
        candidates = [p for p in files if p.name.lower() == 'protocol.json']
        if not candidates:
            raise ApiError(422, 'DATASET_INVALID', '训练协议ZIP缺少 protocol.json')
        if len(candidates) != 1:
            raise ApiError(422, 'DATASET_INVALID', '训练协议ZIP必须只有一份 protocol.json')
        path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ApiError(422, 'DATASET_INVALID', '训练协议输入必须是 JSON 配置或含 protocol.json 的ZIP') from exc
    payload = payload.get('protocol', payload) if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not payload:
        raise ApiError(422, 'DATASET_INVALID', '上传文件没有有效训练协议对象')
    return payload, path.parent


def _download_path(unique_id: str, raw: str) -> Path:
    root = _workspace(unique_id)
    value = raw
    if value.startswith("tmp://"):
        value = value[len("tmp://"):]
        if "/" in value:
            prefix, rest = value.split("/", 1)
            value = rest if prefix == unique_id else value
    if value.startswith("/workspace/"):
        value = value[len("/workspace/"):]
        if "/" in value:
            prefix, rest = value.split("/", 1)
            value = rest if prefix == unique_id else value
    return safe_join(root, value, must_exist=True)


@router.get("/downloadFile")
def download_file(unique_id: str = Query(..., alias="uniqueId"),
                  path: str = Query(...)) -> FileResponse:
    target = _download_path(unique_id, path)
    if target.is_dir():
        raise ApiError(422, "INVALID_INPUT", "path 必须是文件")
    return FileResponse(target, filename=target.name)


@router.post("/downloadFile")
def download_file_post(body: dict[str, Any] = Body(...)) -> FileResponse:
    return download_file(str(body.get("uniqueId") or ""), str(body.get("path") or ""))


@router.post("/removeFile")
def remove_file(body: dict[str, Any] = Body(...)) -> dict:
    unique_id = str(body.get("uniqueId") or "")
    if not is_valid_id(unique_id):
        raise ApiError(400, "INVALID_UNIQUE_ID", "uniqueId 不合法")
    raw = body.get("path")
    if not raw:
        root = _workspace(unique_id)
        if not root.exists():
            return ok({"removed": [], "count": 0})
        removed = [str(item.relative_to(root)) for item in root.rglob("*") if item.is_file()]
        shutil.rmtree(root, ignore_errors=True)
        return ok({"removed": removed, "count": len(removed)})
    target = _download_path(unique_id, str(raw))
    if target.is_dir():
        removed = [str(item.relative_to(target)) for item in target.rglob("*") if item.is_file()]
        shutil.rmtree(target, ignore_errors=True)
    else:
        removed = [target.name]
        target.unlink()
    return ok({"removed": removed, "count": len(removed)})
