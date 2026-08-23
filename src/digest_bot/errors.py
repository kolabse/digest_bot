from __future__ import annotations


class DeliveryOperationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class RetryableDeliveryError(DeliveryOperationError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            message,
            retryable=True,
            retry_after_seconds=retry_after_seconds,
        )


class PermanentDeliveryError(DeliveryOperationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
