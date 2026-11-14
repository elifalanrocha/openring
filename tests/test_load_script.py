"""scripts/load_test.py: the pieces that don't need a run of minutes."""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"


def load():
    spec = importlib.util.spec_from_file_location("load_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_reads_its_own_memory_without_psutil():
    assert 1 < load().memory_mb() < 100_000


def test_it_explains_itself():
    out = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60).stdout
    assert "--levels" in out and "--seconds" in out
