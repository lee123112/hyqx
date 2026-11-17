"""Factory for the standard three-interface augmentation operator API.

无血缘改造（2026-10-19）：/predict 不再返回 lineageFile；成功时返回
保留 outputFile / rejectFile / stageZip / storageUri，并增加统计、样本和拒因。
"""
from __future__ import annotations
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from common.predict_payload import build_predict_fields, extract_sample_limit


class PredictRequest(BaseModel):
    uniqueId: str
    operatorId: Optional[str] = None
    isLastOperator: Optional[StrictBool] = None
    inputFile: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)


def create_app(processor, title: str, output_name: str):
    root = Path(__file__).resolve().parents[1]
    output_root = Path(__import__("os").environ.get("OUTPUT_DIR", root / "work" / "augmentation_outputs"))
    operator_id = str(getattr(processor, "OPERATOR_ID", "") or "")
    payload_operator = str(getattr(processor, "OPERATOR", "") or operator_id)
    app = FastAPI(title=title, version="v1.0.0")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request, _exc):
        """保持请求校验失败也遵循 {code,msg,data} 外层结构。"""
        return JSONResponse(
            status_code=200,
            content={"code": 1, "msg": "请求参数校验失败", "data": None},
        )

    def task(uid: str) -> Path:
        from common.augmentation_runtime import task_dir
        return task_dir(output_root, uid)

    @app.post("/uploadFile")
    async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)):
        try:
            directory = task(uniqueId) / "input"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / Path(file.filename or "input.jsonl").name
            target.write_bytes(await file.read())
            return {"code": 0, "msg": "success", "data": {"inputFile": str(target)}}
        except Exception as exc:
            return {"code": 1, "msg": str(exc), "data": None}

    @app.post("/predict")
    def predict(req: PredictRequest):
        try:
            if req.isLastOperator is None:
                return {"code": 1, "msg": "isLastOperator 必传且必须为布尔值", "data": None}
            requested = str(req.operatorId or "").strip()
            if requested and operator_id and requested != operator_id:
                return {"code": 1, "msg": f"operatorId 不匹配：本算子为 {operator_id}", "data": None}
            directory = task(req.uniqueId)
            input_file = Path(req.inputFile) if req.inputFile else sorted((directory / "input").glob("*.jsonl"))[-1]
            if not input_file.is_absolute(): input_file = directory / input_file
            input_file = input_file.resolve(); input_file.relative_to(directory.resolve())
            overrides = dict(req.config or {})
            overrides.update(req.params or {})
            sample_limit = extract_sample_limit(overrides)
            result = processor.run(str(input_file), req.uniqueId, output_root, overrides=overrides or None)
            if result.get("status") == "failed":
                return {"code": 1, "msg": result.get("message", "处理失败"), "data": None}
            output_path = result.get("outputFile")
            if not output_path or not Path(output_path).is_file():
                return {"code": 1, "msg": "输出文件不可读取", "data": None}
            if req.isLastOperator:
                try:
                    output_value = Path(output_path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return {"code": 1, "msg": "阶段末输出文件内容不可读取", "data": None}
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
                                             operator_id=payload_operator, input_file=input_file))
            return {"code": 0, "msg": "success", "data": data}
        except Exception as exc:
            return {"code": 1, "msg": str(exc), "data": None}

    @app.post("/removeFile")
    def remove_file(uniqueId: str = Query(...)):
        try:
            directory = task(uniqueId)
            shutil.rmtree(directory, ignore_errors=True)
            return {"code": 0, "msg": "success", "data": None}
        except Exception as exc:
            return {"code": 1, "msg": str(exc), "data": None}
    return app
