import logging
from datetime import datetime
from enum import Enum
from time import perf_counter
from uuid import UUID, uuid4
import simplejson as json
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel
from psycopg.errors import DeadlockDetected, SerializationFailure
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response
from app import auth
from app.errors import Failure

LOGGER = logging.getLogger("marketplace.requests")
PUBLIC = {"/health", "/auth/register", "/auth/login", "/auth/refresh", "/docs", "/openapi.json", "/docs/oauth2-redirect"}
SENSITIVE = {"password", "password_hash", "access_token", "refresh_token", "authorization", "secret"}


def json_default(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True)
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError("Unsupported JSON type")


def render(value, status=200):
    if status == 204:
        return Response(status_code=204)
    text = json.dumps(value, default=json_default, use_decimal=True, ensure_ascii=False, allow_nan=False)
    return Response(text, status_code=status, media_type="application/json")


def redact(value):
    if isinstance(value, dict):
        return {key: "***" if key.lower() in SENSITIVE else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class ContractRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            body = await request.body()
            if body:
                if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise Failure(415, "UNSUPPORTED_MEDIA_TYPE", "Требуется application/json")
                try:
                    request._json = json.loads(body, use_decimal=True, allow_nan=False)
                except (ValueError, UnicodeError):
                    raise Failure(400, "VALIDATION_ERROR", "Некорректный JSON") from None
            return await original(request)

        return handle


def install(app):
    @app.exception_handler(Failure)
    async def failure_handler(request, error):
        return render(error.body, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, error):
        fields = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]} for item in error.errors()]
        return render(Failure(400, "VALIDATION_ERROR", "Некорректный запрос", {"fields": fields}).body, 400)

    @app.exception_handler(HTTPException)
    async def http_handler(request, error):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(error.status_code, "VALIDATION_ERROR")
        return render(Failure(error.status_code, code, str(error.detail)).body, error.status_code)

    @app.middleware("http")
    async def audit(request, call_next):
        request_id, started, timestamp = str(uuid4()), perf_counter(), auth.now()
        request.state.actor = None
        body = None
        try:
            if request.method in {"POST", "PUT", "DELETE"}:
                raw = await request.body()
                try:
                    body = redact(json.loads(raw, use_decimal=True)) if len(raw) <= 65536 else "[body exceeds log limit]"
                except (ValueError, UnicodeError):
                    body = "[empty or invalid JSON]"
            if request.url.path not in PUBLIC:
                request.state.actor = await run_in_threadpool(auth.authenticate, request.headers.get("authorization"))
            result = await call_next(request)
        except Failure as error:
            result = render(error.body, error.status)
        except (DeadlockDetected, SerializationFailure):
            result = render(Failure(409, "CONCURRENT_MODIFICATION", "Конфликт транзакций; повторите запрос").body, 409)
        except Exception as error:
            LOGGER.error(json.dumps({"request_id": request_id, "exception_type": type(error).__name__}))
            result = render(Failure(500, "INTERNAL_ERROR", "Внутренняя ошибка сервера").body, 500)
        result.headers["X-Request-Id"] = request_id
        entry = {"request_id": request_id, "method": request.method, "endpoint": request.url.path,
                 "status_code": result.status_code, "duration_ms": round((perf_counter() - started) * 1000, 3),
                 "user_id": request.state.actor["id"] if request.state.actor else None, "timestamp": timestamp}
        if request.method in {"POST", "PUT", "DELETE"}:
            entry["body"] = body
        LOGGER.info(json.dumps(entry, default=json_default, use_decimal=True, ensure_ascii=False))
        return result
