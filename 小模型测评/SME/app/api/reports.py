"""Task and period evaluation reports aligned with the frontend preview."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from ..core.config import settings
from ..core.envelope import ApiError, ok
from ..core.ids import SHANGHAI, now
from ..eval import rules, templates
from ..store import db

router = APIRouter(prefix="/api/v1/eval", tags=["reports"])

PERIODS = {"week": 7, "month": 30, "quarter": 90}
PERIOD_TITLES = {
    "week": "数据集质量周报",
    "month": "数据集质量月度分析",
    "quarter": "季度优化验证报告",
}


def conclusion_of(task: dict[str, Any]) -> str:
    summary = (task.get("result") or {}).get("summary") or {}
    if summary.get('scope') == 'dataset':
        return summary.get('conclusion') or '训练协议结果待业务判定'
    accepted = bool(summary.get("accepted"))
    issues = int(summary.get("issues") or 0)
    if accepted and not issues:
        return "评测达标"
    if accepted:
        return f"批次指标达标 · {issues} 条待优化"
    return "待优化"


def _get_completed_task(task_id: str) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise ApiError(404, "NOT_FOUND", "评测任务不存在")
    if task.get("status") != "completed" or not isinstance(task.get("result"), dict):
        raise ApiError(422, "REQUEST_INVALID", "任务尚未完成，无法生成报告")
    return task


def build_task_report(task: dict[str, Any]) -> dict[str, Any]:
    type_ = str(task.get("type") or "")
    template = templates.TEMPLATES.get(type_) or {}
    result = task.get("result") or {}
    summary = result.get("summary") or {}
    plan_snapshot = task.get("planSnapshot") or {}
    baseline_config = task.get("baselineConfig") or plan_snapshot.get("baselineConfig")
    samples = [
        {
            "id": sample.get("id"),
            "title": sample.get("title") or sample.get("scene") or "—",
            "passed": bool(sample.get("passed")),
            "grade": sample.get("grade"),
            "issues": list(sample.get("issues") or []),
        }
        for sample in result.get("samples") or []
    ]
    report: dict[str, Any] = {
        "title": "数据集评测报告",
        "taskId": task.get("id"),
        "name": task.get("name"),
        "type": type_,
        "typeName": template.get("name"),
        "mode": task.get("mode"),
        "status": task.get("status"),
        "conclusion": conclusion_of(task),
        "generatedAt": now(),
        "createdAt": task.get("createdAt"),
        "completedAt": task.get("completedAt"),
        "dataset": {
            "id": task.get("datasetId"),
            "name": task.get("datasetName"),
            "count": len(task.get("samples") or []),
            "origin": task.get("datasetOrigin"),
        },
        "models": {
            "baselineConfig": baseline_config,
            "id": task.get("modelId") or plan_snapshot.get("modelId"),
            "version": task.get("modelVersion") or plan_snapshot.get("modelVersion"),
            "baseline": task.get("baselineModel"),
            "candidate": task.get("candidateModel"),
        },
        "validationVersion": task.get("validationVersion"),
        "seed": task.get("seed"),
        "config": task.get("config"),
        "parameterSource": task.get("parameterSource"),
        "summary": summary,
        "metrics": summary.get("metrics") or [],
        "samples": samples,
        "feedback": task.get("feedback") or [],
    }
    provenance = result.get("provenance")
    if provenance:
        report["provenance"] = provenance
    engine = result.get("engine")
    if isinstance(engine, dict):
        report["engine"] = engine
    if 'trainingProtocol' in result:
        report['trainingProtocol'] = result['trainingProtocol']
        report['dataset']['count'] = summary.get('total', 0)
    return report


def _protocol_lines(engine: dict[str, Any] | None) -> list[str]:
    if not isinstance(engine, dict):
        return []
    meta = engine.get("meta") if isinstance(engine.get("meta"), dict) else {}
    provenance = engine.get("provenance") if isinstance(
        engine.get("provenance"), dict) else {}
    if not meta and not provenance:
        return []
    lines = ["", "## 测量协议", ""]
    labels = (
        ("engineVersion", "引擎版本"),
        ("engine", "引擎"),
        ("backend", "后端"),
        ("metricNature", "指标性质"),
        ("seed", "随机种子"),
        ("sampleCount", "样本数"),
        ("referenceCorpus", "参考语料"),
        ("durationSeconds", "耗时（秒）"),
    )
    for key, label in labels:
        if meta.get(key) is not None:
            lines.append(f"- {label}：`{json.dumps(meta[key], ensure_ascii=False)}`")
    for key, label in (("modelConfig", "模型/训练配置"),
                       ("trainedCorpus", "训练语料"),
                       ("split", "数据划分"),
                       ("asr", "ASR 后端"),
                       ("labels", "标签")):
        if meta.get(key) is not None:
            lines.append(f"- {label}：`{json.dumps(meta[key], ensure_ascii=False)}`")
    if provenance:
        lines.append("- 指标来源：" + "；".join(
            f"`{key}`={value}" for key, value in sorted(provenance.items())))
    for note in engine.get("notes") or []:
        lines.append(f"- 说明：{note}")
    evaluation = meta.get('evaluation') or {}
    if evaluation.get('policy') == 'heldout':
        lines.append(f"- 汇总仅使用独立验证 {len(evaluation.get('sampleIds') or [])} 条；"
                     f"训练角色 {len(evaluation.get('trainingSampleIds') or [])} 条分数仅供诊断。")
    if str(meta.get("engineVersion", "")).startswith("benchmark-import-"):
        from ..engines.benchmark_analysis import report_lines
        lines += report_lines(meta)
    return lines


def task_report_markdown(report: dict[str, Any]) -> str:
    if 'trainingProtocol' in report:
        from ..experiments.training import protocol_markdown
        return protocol_markdown({'trainingProtocol': report['trainingProtocol'],
                                  'summary': report['summary'], 'engine': report['engine']},
                                 title=report.get('name') or '训练式数据集评测')
    lines = [
        "# 数据集评测报告",
        "",
        f"- 任务名称：{report['name']}",
        f"- 任务编号：{report['taskId']}",
        f"- 评测类型：{report.get('typeName') or report.get('type')}",
        f"- 评测模式：{'Demo 模拟结果' if report['mode'] == 'demo' else '导入实测结果'}",
        f"- 评测结论：{report['conclusion']}",
        f"- 完成时间：{report.get('completedAt') or '—'}",
        "",
    ]
    if report["mode"] == "demo":
        lines += ["> Demo 模拟结果，仅用于演示评测流程。", ""]
    lines += ["## 评测信息", ""]
    models = report.get("models") or {}
    dataset = report.get("dataset") or {}
    lines += [
        f"- 数据集：{dataset.get('name')} · {dataset.get('id')}",
        f"- 评测模型：{models.get('candidate')}",
        f"- 评测基线：{(models.get('baselineConfig') or {}).get('name') or models.get('baseline')}",
        f"- 验证集版本：{report.get('validationVersion')}",
        f"- 随机种子：{report.get('seed')}",
        "",
        "## 指标对比",
        "",
        "| 指标 | 基线 | 待评估 | 变化 | 判定规则 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for metric in report.get("metrics") or []:
        rule = (f"{'≤' if metric.get('direction') == 'lower' else '≥'}"
                f"{metric.get('threshold')}{metric.get('unit') or ''}")
        if report.get("type") == "benchmark":
            rule = "分差 " + rule
        lines.append(
            f"| {metric.get('name')} | {metric.get('baseline')}{metric.get('unit') or ''} "
            f"| {metric.get('candidate')}{metric.get('unit') or ''} "
            f"| {(metric.get('delta') or 0):+g}{metric.get('unit') or ''} "
            f"| {rule} | {'达标' if metric.get('passed') else '未达标'} |"
        )
    lines += _protocol_lines(report.get("engine"))
    lines += ["", "## 逐条结果", "",
              "| 样本 ID | 场景 / 标题 | 结果 | 问题说明 |",
              "|---|---|---|---|"]
    for sample in report.get("samples") or []:
        state = (f"{sample['grade']} 级" if sample.get("grade")
                 else ("达标" if sample.get("passed") else "未达标"))
        lines.append(f"| {sample.get('id')} | {sample.get('title')} | {state} "
                     f"| {'、'.join(sample.get('issues') or []) or '—'} |")
    feedback = report.get("feedback") or []
    if feedback:
        lines += ["", "## 优化建议", ""]
        for item in feedback:
            lines.append(f"- **{item.get('sampleId')}**：{item.get('suggestion') or ''}")
    provenance = report.get("provenance")
    if provenance:
        lines += ["", "## 指标来源", ""]
        for key, value in provenance.items():
            lines.append(f"- {key}：{value}")
    lines.append("")
    return "\n".join(lines)


@router.get("/tasks/{task_id}/report")
def get_task_report(
    task_id: str,
    format: str = Query("json"),  # noqa: A002 - contract field name
) -> dict:
    format = format.strip().lower()
    if format not in ("json", "md"):
        raise ApiError(422, "REQUEST_INVALID", "报告格式仅支持 json/md")
    task = _get_completed_task(task_id)
    report = build_task_report(task)
    if format == "md":
        return ok(task_report_markdown(report))
    return ok(report)


def _completed_in_period(days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(SHANGHAI) - timedelta(days=days)
    summaries, _total = db.list_tasks(status="completed", page=1, page_size=100000)
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        completed_at = summary.get("completedAt")
        if not completed_at:
            continue
        try:
            stamp = datetime.strptime(str(completed_at), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if stamp.replace(tzinfo=SHANGHAI) < cutoff:
            continue
        task = db.get_task(summary["id"])
        if task and isinstance(task.get("result"), dict):
            rows.append(task)
    return rows


def _group(period: str, mode: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [task for task in tasks if task.get("mode") == mode]
    sample_count = sum(
        int((((task.get('result') or {}).get('summary') or {}).get('total') or 0))
        if ((task.get('result') or {}).get('summary') or {}).get('scope') == 'dataset'
        else len(task.get('samples') or []) for task in rows)
    dataset_tasks = sum(
        ((task.get('result') or {}).get('summary') or {}).get('scope') == 'dataset'
        for task in rows)
    grades = {"AA": 0, "A": 0, "B": 0, "C": 0}
    for task in rows:
        summary = (task.get("result") or {}).get("summary") or {}
        for grade in grades:
            grades[grade] += int((summary.get("grades") or {}).get(grade) or 0)
    grade_total = sum(grades.values())
    grade_shares = {
        grade: (rules.round2(count / grade_total * 100) if grade_total else 0)
        for grade, count in grades.items()
    }
    trends = [
        {
            "date": task.get("completedAt"),
            "taskId": task.get("id"),
            "type": task.get("type"),
            "scope": ((task.get('result') or {}).get('summary') or {}).get('scope', 'sample'),
            "metrics": ((task.get("result") or {}).get("summary") or {}).get("metrics") or [],
        }
        for task in rows
    ]
    cases = [
        {
            "taskId": task.get("id"),
            "sampleId": item.get("sampleId"),
            "issue": item.get("issue"),
            "sourceStep": item.get("sourceStep"),
            "status": item.get("status"),
        }
        for task in rows
        for item in (task.get("feedback") or [])
    ]
    optimization = []
    if period == "quarter":
        optimization = [
            {
                "taskId": task.get("id"),
                "sampleId": item.get("sampleId"),
                "status": item.get("status"),
                "verificationId": item.get("verificationId"),
                "parametersBefore": item.get("parameters"),
                "parametersAfter": item.get("parametersAfter"),
                "performanceBefore": item.get("performanceBefore"),
                "performanceAfter": item.get("performanceAfter"),
            }
            for task in rows
            for item in (task.get("feedback") or [])
        ]
    systemic = []
    if period != "week":
        counts: dict[str, int] = {}
        for item in (entry for task in rows for entry in (task.get("feedback") or [])):
            step = str(item.get("sourceStep") or "未标记步骤")
            counts[step] = counts.get(step, 0) + 1
        systemic = [{"step": step, "count": count} for step, count in counts.items()]
    return {
        "mode": mode,
        "taskCount": len(rows),
        "sampleCount": sample_count,
        "scopeTaskCounts": {'sample': len(rows) - dataset_tasks, 'dataset': dataset_tasks},
        "gradedSampleCount": grade_total,
        "grades": grades,
        "gradeShares": grade_shares,
        "trends": trends,
        "cases": cases,
        "systemicIssues": systemic,
        "optimizationVerification": optimization,
    }


@router.get("/reports/period")
def period_report(period: str = Query("week")) -> dict:
    period = period.strip().lower()
    if period not in PERIODS:
        raise ApiError(422, "REQUEST_INVALID", "统计周期仅支持 week/month/quarter")
    days = PERIODS[period]
    tasks = _completed_in_period(days)
    return ok({
        "title": PERIOD_TITLES[period],
        "generatedAt": now(),
        "periodDays": days,
        "scope": "服务端保存的已完成评测任务",
        "note": "模拟指标与导入实测指标分开汇总，人工复核不改写原始模型测量。",
        "groups": [_group(period, mode, tasks) for mode in ("demo", "measured")],
    })


@router.get("/console", include_in_schema=False)
def console_page() -> FileResponse:
    """Serve the single-file integration console from the same origin (CORS-free)."""
    path = settings.project_root / "web" / "console.html"
    if not path.is_file():
        raise ApiError(404, "NOT_FOUND", "联调台页面不存在")
    return FileResponse(str(path), media_type="text/html; charset=utf-8")
