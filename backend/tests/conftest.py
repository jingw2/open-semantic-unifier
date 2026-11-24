"""Test isolation helpers loaded before test modules import the application."""
import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="open-semantic-unifier-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT / 'platform.db'}"
os.environ["LOCAL_STORAGE_DIR"] = str(_TEST_ROOT / "storage")
os.environ["CELERY_ENABLED"] = "false"
os.environ["STORAGE_BACKEND"] = "local"
