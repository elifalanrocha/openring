"""The release workflow publishes one version everywhere; these checks catch a mismatch before a tag does."""

import json
import re
import tomllib
from pathlib import Path

import openring

ROOT = Path(__file__).resolve().parent.parent


def test_every_version_number_matches():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    assert pyproject == package == openring.__version__


def test_launcher_installs_its_own_version_from_pypi():
    launcher = (ROOT / "bin" / "openring.js").read_text(encoding="utf-8")
    assert 'require("../package.json").version' in launcher
    assert re.search(r"openring\[kokoro,whisper\]==\$\{VERSION\}", launcher)
    assert "OPENRING_SOURCE" in launcher  # a local checkout can still be run


def test_third_party_lists_every_direct_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    notices = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8").lower()
    extras = project["optional-dependencies"]
    deps = project["dependencies"] + extras["kokoro"] + extras["whisper"]
    names = {re.match(r"[A-Za-z0-9_.-]+", d)[0].lower() for d in deps}
    assert {n for n in names if f"[{n}]" not in notices} == set()


def test_release_workflow_uses_trusted_publishing():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish" in workflow and "npm stage publish . --provenance" in workflow
    assert "secrets." not in workflow  # no stored tokens


def test_launcher_asks_pypi_again_a_few_times_when_a_new_release_is_not_there_yet():
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js")
    launcher = (ROOT / "bin" / "openring.js").read_text(encoding="utf-8")
    start = launcher.index("const STALE_INDEX")
    code = launcher[start:launcher.index("let waiting")]
    code = code.replace("process.env.OPENRING_RETRY_SECONDS ?? 20", "20")  # as shipped
    checks = """
    const stale = "error: No solution found when resolving tool dependencies: no version of openring==0.1.7";
    const other = "ModuleNotFoundError: No module named 'x'";
    console.log(JSON.stringify({
      first: shouldRetry(1, 0, stale, false), third: shouldRetry(1, 2, stale, false),
      giveUp: shouldRetry(1, 3, stale, false), ok: shouldRetry(0, 0, stale, false),
      otherError: shouldRetry(1, 0, other, false), fromSource: shouldRetry(1, 0, stale, true),
      wait: RETRY_WAIT_MS, tries: RETRIES,
    }));
    """
    out = subprocess.run([node, "-e", code + checks], capture_output=True, text=True, timeout=30, check=True).stdout
    got = json.loads(out)
    assert got == {"first": True, "third": True, "giveUp": False, "ok": False, "otherError": False,
                   "fromSource": False, "wait": 20000, "tries": 3}
    assert "if (waiting) process.exit(130)" in launcher  # Ctrl+C during the pause leaves at once



def test_the_docker_image_keeps_downloaded_models_on_the_data_volume():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    env = next(line for line in dockerfile.splitlines() if line.startswith("ENV "))
    assert "HOME=/data" in env and "OPENRING_HOME=/data" in env  # ~/.cache is on the volume
    assert 'VOLUME ["/data"]' in dockerfile
