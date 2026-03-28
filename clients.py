"""Centralized cloud clients for the project.

This module owns runtime client objects (S3/R2, databases, etc.). Tests
can safely monkeypatch attributes on this module (for example
`monkeypatch.setattr(clients, 's3', mock)`) without poking into
implementation modules.
"""
from __future__ import annotations

import boto3
import config


# Public client objects (may be None when not configured)
s3 = None


def init_clients() -> None:
    """Initialize cloud clients based on runtime configuration.

    This function is idempotent and safe to call multiple times. It reads
    `config.STORAGE_BACKEND` to decide whether to create an S3/R2 client.
    """
    global s3

    if config.STORAGE_BACKEND == "r2":
        s3 = boto3.client(
            "s3",
            aws_access_key_id=config.R2_ACCESS_KEY,
            aws_secret_access_key=config.R2_SECRET_KEY,
            endpoint_url=config.R2_ENDPOINT_URL,
        )


# Auto-initialize on import to preserve previous behaviour where the
# client was created at module import time.
init_clients()
