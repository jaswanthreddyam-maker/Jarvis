from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    jitter_seconds: float = 0.2

    def delay_for(self, attempt_index: int) -> float:
        exponential = self.backoff_base_seconds * (2 ** max(0, attempt_index - 1))
        capped = min(self.backoff_max_seconds, exponential)
        if self.jitter_seconds <= 0:
            return capped
        return capped + random.uniform(0.0, self.jitter_seconds)


def retry_call(
    operation: Callable[[], T],
    *,
    operation_name: str,
    policy: RetryPolicy,
    logger: logging.Logger,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
) -> T:
    last_error: BaseException | None = None
    attempts = max(1, policy.max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retryable_exceptions as exc:
            last_error = exc
            retry_allowed = should_retry(exc) if should_retry is not None else True
            if not retry_allowed or attempt >= attempts:
                raise
            delay = policy.delay_for(attempt)
            logger.warning(
                "%s failed on attempt %d/%d: %s",
                operation_name,
                attempt,
                attempts,
                exc,
                extra={
                    "event": "retry.scheduled",
                    "operation": operation_name,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(exc).__name__,
                },
            )
            time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{operation_name} failed without an exception.")


async def retry_call_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    policy: RetryPolicy,
    logger: logging.Logger,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
) -> T:
    last_error: BaseException | None = None
    attempts = max(1, policy.max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retryable_exceptions as exc:
            last_error = exc
            retry_allowed = should_retry(exc) if should_retry is not None else True
            if not retry_allowed or attempt >= attempts:
                raise
            delay = policy.delay_for(attempt)
            logger.warning(
                "%s failed on attempt %d/%d: %s",
                operation_name,
                attempt,
                attempts,
                exc,
                extra={
                    "event": "retry.scheduled",
                    "operation": operation_name,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(exc).__name__,
                },
            )
            await asyncio.sleep(delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{operation_name} failed without an exception.")
