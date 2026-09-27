import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

log = logging.getLogger("permetheus")

CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details


def error_response(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return error_response(exc.status_code, CODES.get(exc.status_code, "error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        details = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        return error_response(422, "validation_error", "Request validation failed", details)

    @app.exception_handler(OperationalError)
    async def db_unavailable(_: Request, exc: OperationalError):
        log.error("database error: %s", exc.orig)
        return error_response(503, "database_unavailable", "Database is unavailable")

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "Internal server error")
