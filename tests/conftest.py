import os
import sys
import importlib


def pytest_addoption(parser):
    parser.addoption(
        "--storage-backend",
        action="store",
        default=None,
        choices=("local", "r2"),
        help="Select storage backend for tests (overrides STORAGE_BACKEND env var).",
    )


def pytest_configure(config):
    # Determine backend from CLI option or environment (default to local)
    backend = config.getoption("--storage-backend") or os.getenv("STORAGE_BACKEND", "local")
    os.environ["STORAGE_BACKEND"] = backend

    # If modules have already been imported, reload them so they pick up the new env/config
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    if "storage" in sys.modules:
        importlib.reload(sys.modules["storage"])
