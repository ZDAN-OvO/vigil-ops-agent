"""本地服务共用的请求上限与错误响应。"""

from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse


class APIError(HTTPException):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(status, detail=message)
        self.code = code


async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    code = (
        exc.code
        if isinstance(exc, APIError)
        else {
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "request_failed")
    )
    return JSONResponse(
        {"error": code, "message": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


async def read_body(request: Request, *, media_type: str, limit: int) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != media_type:
        raise APIError(415, "invalid_content_type", "请求类型错误")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise APIError(413, "body_too_large", "请求过大")
            body.extend(chunk)
    except ClientDisconnect:
        raise APIError(400, "invalid_body", "请求未完成") from None
    return bytes(body)


async def read_json[T: BaseModel](request: Request, model: type[T], *, limit: int) -> T:
    body = await read_body(request, media_type="application/json", limit=limit)
    try:
        return model.model_validate_json(body, strict=True)
    except ValidationError:
        raise APIError(400, "invalid_body", "请求字段或格式错误") from None
