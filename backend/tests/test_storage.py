"""Local object storage size reads used by bounded exports."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.storage import StorageService


def test_local_get_size_matches_stored_bytes(tmp_path):
    service = StorageService()
    service.backend = "local"
    service.local_root = str(tmp_path)
    service.put_bytes("projects/one/instances.csv", b"id,name\n1,Acme\n")
    assert service.get_size("projects/one/instances.csv") == len(b"id,name\n1,Acme\n")
