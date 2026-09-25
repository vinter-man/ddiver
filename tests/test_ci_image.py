"""CI must prove the image runs before deploy ships it, not only that it builds.

Since ddiver became a workspace the image installs its packages as wheels (``--no-editable``),
while the check job tests the editable install. A hatch ``packages`` setting that drops a
subpackage still builds; only importing from the installed wheels shows it. These assertions
keep that smoke step in the job deploy waits for, and keep the dev group out of the image.
"""

from __future__ import annotations

import importlib.util
import sys
import sysconfig
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
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


def test_the_check_job_has_b25b311_to_rebuild_the_upgrade_fixtures():
    """checkout@v4 fetches one commit unless told otherwise, and without b25b311 in the clone
    the test that ties tests/fixtures/b25b311 to b25b311's own code cannot run. The check job
    is the one deploy waits for, so it is the one that needs the history."""
    assert "check" in jobs()["deploy"]["needs"]
    checkouts = [
        step
        for step in jobs()["check"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert (checkouts[0].get("with") or {}).get("fetch-depth") == 0


def test_the_image_job_imports_every_module_of_both_packages_offline():
    assert "--network none --entrypoint python ddiver:ci" in smoke()
    script = smoke_script()
    assert "walk_packages" in script
    assert "for package in (ddiver, ddiver_core):" in script


def test_the_smoke_script_is_python():
    """A quoting slip in YAML or the shell turns it into a SyntaxError only CI would see."""
    compile(smoke_script(), "<smoke>", "exec")


def test_the_image_job_imports_the_wheels_not_the_copied_sources():
    """`python -c` puts the working directory first on sys.path, and the image's WORKDIR, /app,
    holds a copy of the sources. Without -P, `import ddiver` found /app/ddiver and walked the
    source tree, so a wheel missing a module passed the smoke step."""
    assert "--network none --entrypoint python ddiver:ci -P -c '" in smoke()
    script = smoke_script()
    assert 'installed = sysconfig.get_paths()["purelib"] + "/"' in script
    assert "assert where.startswith(installed)" in script


def run_smoke_script(monkeypatch, files: dict[str, str]) -> None:
    """Run the smoke script here, with ddiver and ddiver_core stood in for by empty packages
    that claim to live at the given files."""
    for name, where in files.items():
        stub = types.ModuleType(name)
        stub.__file__ = where
        stub.__path__ = []
        monkeypatch.setitem(sys.modules, name, stub)
    exec(compile(smoke_script(), "<smoke>", "exec"), {})  # noqa: S102 - our own CI script


def as_built() -> dict[str, str]:
    installed = sysconfig.get_paths()["purelib"]
    return {name: f"{installed}/{name}/__init__.py" for name in ("ddiver", "ddiver_core")}


def test_the_smoke_script_refuses_a_package_imported_from_the_sources(monkeypatch):
    """What the image job saw before -P: ddiver from /app/ddiver, ddiver_core from the venv."""
    files = as_built() | {"ddiver": "/app/ddiver/__init__.py"}
    with pytest.raises(AssertionError, match=r"^ddiver is not the installed wheel: /app/ddiver/"):
        run_smoke_script(monkeypatch, files)


def test_the_smoke_script_refuses_the_dev_group(monkeypatch):
    """This environment has pytest and the shared fakes, exactly what the image must not."""
    with pytest.raises(AssertionError, match=r"dev-only packages in the image: .*pytest"):
        run_smoke_script(monkeypatch, as_built())


def test_the_smoke_script_passes_an_image_as_built(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    run_smoke_script(monkeypatch, as_built())


def test_the_image_job_checks_the_dev_group_did_not_come_along():
    script = smoke_script()
    assert 'dev_only = ("pytest",' in script
    assert "shipped = [name for name in dev_only if importlib.util.find_spec(name)]" in script
    assert "assert not shipped" in script


def test_the_image_job_runs_the_entry_point():
    assert "--network none ddiver:ci --help" in smoke()


def test_the_shared_fakes_never_reach_the_image():
    """ddiver-core-testing is a dev dependency. uv needs its pyproject to read the workspace;
    nothing else of it may enter the build context, and the smoke step proves it is not in the
    venv."""
    dev_only = smoke_script().split("dev_only = ", 1)[1].splitlines()[0]
    assert '"ddiver_core_testing"' in dev_only
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "packages/ddiver-core-testing/src/" in ignored
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY packages/ddiver-core-testing/pyproject.toml" in dockerfile
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "ddiver-core-testing" in pyproject["dependency-groups"]["dev"]
    core = tomllib.loads((ROOT / "packages/ddiver-core/pyproject.toml").read_text(encoding="utf-8"))
    for dependencies in (pyproject["project"]["dependencies"], core["project"]["dependencies"]):
        assert not [d for d in dependencies if d.startswith("ddiver-core-testing")]
