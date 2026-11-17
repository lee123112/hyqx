"""FastAPI 服务：uploadFile / predict / removeFile 三接口。

外层响应统一为 {code,msg,data}，不提供 downloadFile。
无血缘改造（2026-10-19）：不再生成/返回血缘文件；/predict 成功时返回
outputFile / rejectFile / stageZip / storageUri 四个原字段及统计、样本、拒因四个新增字段。
"""
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from processor import run
from utils.io import ensure_dir, safe_join, validate_unique_id
from utils.schemas import PredictRequest
from utils.predict_payload import build_predict_fields, extract_sample_limit

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output")).resolve()
OPERATOR_ID = "12"

app = FastAPI(title="算子12 双语对齐置信度", version="v1.0.0")


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


def _task_path(unique_id: str) -> Path:
    """解析任务目录但不创建，避免非法/无输入的调用留下空目录。"""
    value = validate_unique_id(unique_id)
    target = (OUTPUT_ROOT / value).resolve()
    try:
        target.relative_to(OUTPUT_ROOT)
    except ValueError as exc:
        raise ValueError("uniqueId 指向任务目录之外") from exc
    return target


def _resolve_input_file(unique_id: str, input_file: Optional[str]) -> Optional[Path]:
    value = validate_unique_id(unique_id)
    tdir = _task_path(value)
    if not input_file:
        # 阶段首算子不传 inputFile：取上传目录中最后写入的文件。
        candidates = sorted(p for p in (tdir / "input").glob("*") if p.is_file())
        return candidates[-1] if candidates else None
    raw = Path(input_file)
    candidates = [raw] if raw.is_absolute() else [tdir / raw, BASE_DIR / raw]
    for candidate in candidates:
        target = candidate.resolve()
        try:
            target.relative_to(tdir)
        except ValueError:
            continue
        if target.is_file():
            return target
    return None


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
                                     operator_id="12", input_file=input_file))
    return data, None


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)):
    try:
        task = _task_path(uniqueId)
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
    operator_id = str(getattr(req, "operatorId", None) or "").strip()
    if operator_id and operator_id != OPERATOR_ID:
        return _failed(f"operatorId 不匹配：本算子为 {OPERATOR_ID}")
    if not req.inputFile:
        return _failed("阶段内后续算子必须传 inputFile")
    try:
        input_path = _resolve_input_file(req.uniqueId, req.inputFile)
    except ValueError as exc:
        return _failed(str(exc))
    if input_path is None:
        message = "未找到输入文件，请先调用 /uploadFile 或传入任务目录内的 inputFile"
        if req.inputFile:
            message = "未找到 inputFile 指向的文件，请使用上一算子返回的临时 outputFile"
        return _failed(message)
    overrides = dict(req.config or {})
    overrides.update(req.params or {})
    try:
        sample_limit = extract_sample_limit(overrides)
    except ValueError as exc:
        return _failed(str(exc))
    result = run(str(input_path), req.uniqueId, OUTPUT_ROOT, overrides=overrides or None)
    data, error = _predict_data(result, req.isLastOperator, sample_limit, input_path)
    if error:
        return _failed(error)
    return _ok(data)


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(...)):
    try:
        tdir = _task_path(uniqueId)
    except ValueError as exc:
        return _failed(str(exc))
    shutil.rmtree(tdir, ignore_errors=True)
    return {"code": 0, "msg": "success"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8012)

