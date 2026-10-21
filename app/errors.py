from openapi_server.models.api_error import ApiError


class Failure(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        if code == "VALIDATION_ERROR" and details is None:
            details = {"fields": [{"field": "request", "message": message}]}
        self.body = ApiError(error_code=code, message=message, details=details or {})


def require(condition, status, code, message, details=None):
    if not condition:
        raise Failure(status, code, message, details)


def role(actor, *allowed):
    require(actor is not None and actor["role"] in allowed,
            403, "ACCESS_DENIED", "Недостаточно прав")
