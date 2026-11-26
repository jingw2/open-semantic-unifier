"""L1 fingerprint IDs must be stable across processes and restarts."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.engines import stable_entity_id


def test_stable_entity_id_is_reproducible_across_processes():
    script = "import sys; sys.path.insert(0, 'backend'); from app.services.engines import stable_entity_id; print(stable_entity_id('深圳市腾讯计算机系统有限公司'))"
    first = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()
    second = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()
    assert first == second
    assert stable_entity_id("深圳市腾讯计算机系统有限公司") == first
    assert first.startswith("ENT-")
