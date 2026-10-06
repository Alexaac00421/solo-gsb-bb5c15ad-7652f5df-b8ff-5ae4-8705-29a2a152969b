class ApiError(Exception):
    """业务错误，由全局异常处理器转成对应 HTTP 状态码。"""

    status = 400

    def __init__(self, message: str, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.extra = extra or {}


class BadRequest(ApiError):
    status = 400


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409
