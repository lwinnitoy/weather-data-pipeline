from tenacity import (
    retry,
    retry_if_exception_type,
    wait_exponential,
    stop_after_attempt,
    before_sleep_log,
    after_log,
)
import logging
from functools import wraps

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RetryError(Exception):
    """Base class for retryable errors."""


# Deterministic backoff settings for consistent behavior in tests and production
RETRY_MAX_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.5  # seconds
RETRY_MULTIPLIER = 2.0
RETRY_MAX_DELAY = 2.0  # Cap delay at 2 seconds

# Retryable HTTP status codes per retry matrix
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}

# Transient S3/R2 error codes per retry matrix
TRANSIENT_S3_ERROR_CODES = {
    "Throttling",
    "SlowDown",
    "RequestTimeout",
    "InternalError",
    "ServiceUnavailable",
    "ConnectTimeout",
    "SocketTimeout",
}


def is_transient_s3_error(error_code: str) -> bool:
    """Classify S3/R2 ClientError as transient or permanent.
    
    Transient errors are temporary and may succeed on retry (throttling, timeouts, etc).
    Permanent errors require code/config changes to resolve (auth, validation, etc).
    
    Args:
        error_code: S3 error code from ClientError.response["Error"]["Code"]
    
    Returns:
        True if error is transient and should trigger retry, False otherwise
    """
    return error_code in TRANSIENT_S3_ERROR_CODES


def is_retryable_http_status(status_code: int) -> bool:
    """Classify HTTP status code as retryable or not.
    
    Args:
        status_code: HTTP response status code
    
    Returns:
        True if status should trigger retry, False otherwise
    """
    return status_code in RETRYABLE_HTTP_STATUS


def run_with_retry(func=None, retry_exceptions=(RetryError,)):
    """Decorator factory to retry a function on selected exceptions with exponential backoff.
    
    Applies Tenacity with deterministic backoff settings. Logs before each retry
    and after final outcome.
    
    Args:
        func: Callable to decorate (when used without parentheses)
        retry_exceptions: Tuple of exception types to retry
    
    Returns:
        Decorated function with retry behavior
    """
    def decorator(inner_func):
        @retry(
            retry=retry_if_exception_type(retry_exceptions),
            wait=wait_exponential(
                multiplier=RETRY_BASE_DELAY,
                exp_base=RETRY_MULTIPLIER,
                min=RETRY_BASE_DELAY,
                max=RETRY_MAX_DELAY,
            ),
            stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            after=after_log(logger, logging.INFO),
            reraise=True,
        )
        @wraps(inner_func)
        def wrapper(*args, **kwargs):
            return inner_func(*args, **kwargs)

        return wrapper

    if func is None:
        return decorator

    return decorator(func)