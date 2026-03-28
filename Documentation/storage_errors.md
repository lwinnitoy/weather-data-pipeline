Storage error handling
======================

This document describes the storage error model used by `storage.py` and how
calling code (ETL orchestrator, tests, and other modules) should interpret and
handle each error class.

Design goals
------------
- Predictability: Clear, well-documented contracts for return values vs exceptions.
- Fail-fast: Operational failures should surface as exceptions, not silent `None`.
- Recoverability: Distinguish retriable failures from permanent ones.

Exception hierarchy
-------------------
- `StorageError` (base): unexpected operational failures (permissions, decode errors,
  permission denied, internal bugs). Treat as fatal for this operation.

- `NotFoundError` (subclass of `StorageError`): the requested object was not found.
  Use this to signal the normal 'missing' case. Some public functions return
  `None` instead of raising `NotFoundError` — see function docstrings for the
  exact contract.

- `TransientError` (subclass of `StorageError`): transient, retriable failures
  such as network timeouts or temporary service unavailability. Callers can
  implement retry/backoff logic when catching this error.

When to return `None` vs raise
------------------------------
- `read_*` functions (e.g. `read_staging`, `get_high_water_mark`):
  - If the object is simply missing, the public API returns `None` (this is a
    common, expected case). Callers should treat `None` as "nothing to process".
  - If an operational failure occurs while accessing or decoding the object,
    raise `StorageError` or `TransientError`.

- `write_*` functions (e.g. `write_staging`, `write_raw`):
  - On success return the written location (Path or key string).
  - On failure raise `StorageError` (or `TransientError` if retriable). Do not
    return `None` to indicate failure.

- `list_*` functions: return an empty list when nothing is found. Raise
  `StorageError` for operational failures.

Mapping S3/R2 errors
-------------------
When interacting with S3-compatible APIs (Cloudflare R2) via `boto3`, catch
`botocore.exceptions.ClientError` and inspect the response code:

- `NoSuchKey` or HTTP 404: treat as "not found" and return `None` (for reads)
  or empty list (for list operations).
- 5xx errors or connection timeouts: wrap with `TransientError` to signal
  retriable behavior.
- 4xx errors (other than 404): treat as `StorageError` (permission denied,
  invalid credentials, etc.).

Best-practice handling in callers
--------------------------------
- ETL orchestrator should:
  - Call `read_staging(...)` and if it returns `None`, skip processing for that
    partition.
  - Catch `TransientError` and perform a limited retry with exponential backoff.
  - Catch `StorageError` and alert/log as appropriate (do not silently ignore).

- Tests should mock `boto3` to raise `ClientError` variants and assert the
  storage layer maps them to the expected return values or exceptions.

Example: read helper pattern
---------------------------
```py
from botocore.exceptions import ClientError

try:
    resp = s3.get_object(Bucket=bucket, Key=key)
    data = resp["Body"].read()
    return parse(data)
except ClientError as e:
    code = e.response.get("Error", {}).get("Code")
    if code in ("NoSuchKey", "404"):
        return None
    raise TransientError("S3 get_object failed", e)
except Exception as e:
    raise StorageError("Failed to decode object", e)
```

Notes for this repository
-------------------------
- See `storage.py` docstrings for the precise contract of each public function.
- Tests in `tests/` should follow the error model above and assert correct
  behavior for missing objects and exceptional cases.

Contact & Further Reading
-------------------------
- AWS S3 error model: https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html
- Boto3 / Botocore exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/exceptions.html

