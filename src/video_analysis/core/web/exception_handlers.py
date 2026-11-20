from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from video_analysis.core.common.errors import NotFound


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(IntegrityError, _conflict)


async def _not_found(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=404)


async def _conflict(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse({"detail": "数据冲突：名称或版本重复，或仍被其他配置引用"}, status_code=409)
