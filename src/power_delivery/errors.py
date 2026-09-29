"""制造交付编排服务向 API 和 CLI 暴露的稳定错误。"""


class DeliveryError(RuntimeError):
    code = "delivery_error"
    status = 400


class NotFound(DeliveryError):
    code = "not_found"
    status = 404


class Conflict(DeliveryError):
    code = "conflict"
    status = 409

    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class Forbidden(DeliveryError):
    code = "forbidden"
    status = 403


class InvalidState(DeliveryError):
    code = "invalid_state"
    status = 409


class ValidationFailed(DeliveryError):
    code = "validation_failed"
    status = 422
