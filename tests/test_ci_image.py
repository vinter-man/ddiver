"""CI must prove the image runs before deploy ships it, not only that it builds.

Since ddiver became a workspace the image installs its packages as wheels (``--no-editable``),
while the check job tests the editable install. A hatch ``packages`` setting that drops a
subpackage still builds; only importing from the installed wheels shows it. These assertions
keep that smoke step in the job deploy waits for, and keep the dev group out of the image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def jobs() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    found = loaded["jobs"]
    assert isinstance(found, dict)
    return found


def smoke() -> str:
    """Every command the image job runs after it has built the image."""
    runs = [str(step.get("run", "")) for step in jobs()["image"]["steps"]]
    build = next(i for i, run in enumerate(runs) if "docker build" in run)
    return "\n".join(runs[build + 1 :])


def smoke_script() -> str:
    """The Python the smoke step hands to the image's interpreter."""
    text = smoke()
    start = text.index("-c '") + len("-c '")
    return text[start : text.index("\n'", start)]


def test_deploy_waits_for_the_image_job():
    assert "image" in jobs()["deploy"]["needs"]


def test_the_image_job_imports_every_module_of_both_packages_offline():
    assert "--network none --entrypoint python ddiver:ci" in smoke()
    script = smoke_script()
    assert "walk_packages" in script
    assert "for package in (ddiver, ddiver_core):" in script


def test_the_smoke_script_is_python():
    """A quoting slip in YAML or the shell turns it into a SyntaxError only CI would see."""
    compile(smoke_script(), "<smoke>", "exec")


def test_the_image_job_checks_the_dev_group_did_not_come_along():
    script = smoke_script()
    assert 'dev_only = ("pytest",' in script
    assert "shipped = [name for name in dev_only if importlib.util.find_spec(name)]" in script
    assert "assert not shipped" in script


def test_the_image_job_runs_the_entry_point():
    assert "--network none ddiver:ci --help" in smoke()
