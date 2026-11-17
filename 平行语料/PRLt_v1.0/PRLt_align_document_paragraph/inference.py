"""算子9 FastAPI 服务：uploadFile / predict / removeFile。

无血缘改造（2026-10-19）：不再生成/读取/返回血缘文件；`/predict` 成功时返回
outputFile / rejectFile / stageZip / storageUri 四个原字段及统计、样本、拒因四个新增字段。

本算子的输入是「文档/段落级对齐计划」（JSON 计划对象文件），支持两种定位方式：
1. `inputFile`：任务目录内的计划文件（保留原有解析逻辑）；
2. `params.planDirectory`：计划目录，目录下需有 `alignment_plan.json`
   或 `10_align_plan.json`，计划引用的源/目标文档也允许落在该目录内。
"""
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from processor import DEFAULT_PLAN_FILE, run
from utils.io import ensure_dir, task_dir
from utils.schemas import PredictRequest
from utils.predict_payload import build_predict_fields, extract_sample_limit

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output")).resolve()
OPERATOR_ID = "09"
PLAN_FILE_CANDIDATES = ("alignment_plan.json", DEFAULT_PLAN_FILE)

app = FastAPI(title="算子9 文档/段落级对齐", version="v1.0.0")


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, _exc):
    """保持请求校验失败也遵循 MTY 的 {code,msg,data} 外层结构。"""
    return JSONResponse(
        status_code=200,
        content={"code": 1, "msg": "请求参数校验失败", "data": None},
    )


def _ok(data):
    return {"code": 0, "msg": "success", "data": data}


def _failed(message: str):
    return {"code": 1, "msg": message, "data": None}


def _extra_input_root(unique_id: str,
                      input_file: Optional[str]) -> Optional[Path]:
    """Derive an extra allowed root from an upstream inputFile inside the task dir."""
    if not input_file:
        return None
    tdir = task_dir(OUTPUT_ROOT, unique_id).resolve()
    raw = Path(input_file)
    candidate = raw if raw.is_absolute() else tdir / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(tdir)
    except ValueError:
        return None
    return resolved.parent


def _resolve_plan_directory(unique_id: str, plan_directory: str) -> Path:
    """解析 params.planDirectory：只允许任务目录内的目录。"""
    value = task_dir(OUTPUT_ROOT, unique_id).resolve()
    raw = Path(str(plan_directory))
    candidate = (raw if raw.is_absolute() else value / raw).resolve()
    try:
        candidate.relative_to(value)
    except ValueError as exc:
        raise ValueError("planDirectory 指向任务目录之外") from exc
    if not candidate.is_dir():
        raise ValueError("planDirectory 不是有效目录")
    return candidate


# [无血缘改造 2026-10-19] 原 _lineage_payload() 读取血缘文件并组装 data.lineageFile，
# 已停用；保留原实现注释便于回溯。
# def _lineage_payload(path: Optional[str], is_last: bool):
#     ...


def _output_content(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _predict_data(result: dict, is_last: bool, sample_limit=20, input_file=None):
    """组装规范返回：outputFile / rejectFile / stageZip / storageUri（无 lineageFile）。"""
    if result.get("status") == "failed":
        return None, result.get("message") or "处理失败"
    output_path = result.get("outputFile")
    if not output_path or not Path(output_path).is_file():
        return None, "输出文件不可读取"
    if is_last:
        content = _output_content(output_path)
        if content is None:
            return None, "阶段末输出文件内容不可读取"
        output_value = content
    else:
        output_value = str(output_path)
    reject_path = result.get("rejectFile")
    data = {
        "outputFile": output_value,
        "rejectFile": str(reject_path) if reject_path else None,
        "stageZip": result.get("stageZip"),
        "storageUri": result.get("storageUri"),
    }
    data.update(build_predict_fields(result, sample_limit=sample_limit,
                                     operator_id="08", input_file=input_file))
    return data, None


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)):
    try:
        task = task_dir(OUTPUT_ROOT, uniqueId)
    except ValueError as exc:
        return _failed(str(exc))
    filename = Path(file.filename or "").name
    if not filename or filename in {".", ".."}:
        return _failed("上传文件名不能为空")
    input_dir = ensure_dir(task / "input")
    dest = input_dir / filename
    try:
        with open(dest, "wb") as output:
            shutil.copyfileobj(file.file, output)
    except (OSError, ValueError) as exc:
        return _failed(f"上传文件失败：{type(exc).__name__}")
    return {"code": 0, "msg": "success"}


@app.post("/predict")
async def predict(req: PredictRequest):
    if req.isLastOperator is None:
        return _failed("isLastOperator 必传，必须为 bool")
    operator_id = str(req.operatorId or "").strip()
    if operator_id and operator_id != OPERATOR_ID:
        return _failed(f"operatorId 不匹配：本算子为 {OPERATOR_ID}")

    params = dict(req.params or {})
    plan_directory = params.pop("planDirectory", None)
    overrides = dict(req.config or {})
    overrides.update(params)
    try:
        sample_limit = extract_sample_limit(overrides)
    except ValueError as exc:
        return _failed(str(exc))

    try:
        extra_root = _extra_input_root(req.uniqueId, req.inputFile)
    except ValueError as exc:
        return _failed(str(exc))

    plan_file = req.inputFile
    if plan_directory:
        try:
            plan_dir = _resolve_plan_directory(req.uniqueId, plan_directory)
        except ValueError as exc:
            return _failed(str(exc))
        plan_path = next((plan_dir / name for name in PLAN_FILE_CANDIDATES
                          if (plan_dir / name).is_file()), None)
        if plan_path is None:
            return _failed("planDirectory 下未找到 alignment_plan.json 或 "
                           + DEFAULT_PLAN_FILE)
        plan_file = str(plan_path)
        extra_root = plan_dir
    if not plan_file:
        return _failed("阶段内后续算子必须传 inputFile，"
                       "或通过 params.planDirectory 指定计划目录")

    result = run(
        req.uniqueId,
        plan_file=plan_file,
        output_root=OUTPUT_ROOT,
        overrides=overrides or None,
        extra_input_root=extra_root,
    )
    data, error = _predict_data(result, req.isLastOperator, sample_limit, plan_file)
    if error:
        return _failed(error)
    return _ok(data)


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(...)):
    try:
        tdir = task_dir(OUTPUT_ROOT, uniqueId)
    except ValueError as exc:
        return _failed(str(exc))
    shutil.rmtree(tdir, ignore_errors=True)
    return {"code": 0, "msg": "success"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)
