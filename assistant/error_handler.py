from __future__ import annotations

import functools
import inspect
import logging
from pathlib import Path

from assistant.settings import load_structured_config

logger = logging.getLogger("Jarvis.ErrorHandler")


class ErrorHandler:
    def __init__(self, path: Path) -> None:
        config = load_structured_config(path)
        self._default_attempts = int(config.get("default", {}).get("max_attempts", 1))
        self._policies = config.get("policies", {})

    def max_attempts_for(self, action_name: str) -> int:
        policy = self._policies.get(action_name, {})
        return max(1, int(policy.get("max_attempts", self._default_attempts)))


def retry_on_failure(retries: int = 3, fallback=None):
    """
    Synchronous retry decorator.
    Wraps a function so it is retried up to *retries* times on exception.
    If all attempts fail and *fallback* is provided, it is called instead.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max(1, retries)):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "%s attempt %d/%d failed: %s",
                        func.__name__, attempt + 1, retries, exc,
                    )
            if fallback:
                return fallback(*args, **kwargs)
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"{func.__name__} failed without raising a concrete exception.")

        return wrapper

    return decorator


def async_retry_on_failure(retries: int = 3, fallback=None):
    """
    Async retry decorator.
    Same as retry_on_failure but for async functions.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max(1, retries)):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "%s attempt %d/%d failed: %s",
                        func.__name__, attempt + 1, retries, exc,
                    )
            if fallback:
                return await fallback(*args, **kwargs) if inspect.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"{func.__name__} failed without raising a concrete exception.")

        return wrapper

    return decorator
