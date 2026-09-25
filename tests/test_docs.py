"""The documents that say where the code is have to be true.

A module list goes stale the first time a file moves, and then it costs more than it saved: the
reader trusted it and spent their afternoon in the wrong package. DESIGN.md described a
notification button that was never built and the description outlived the decision by months —
that is the failure these assertions exist to prevent, and the same kind of cheap check as
tests/test_packaging.py.

CLAUDE.md is held to the stricter standard of the three, because an agent reads it first and
acts on it without asking.
"""

from __future__ import annotations

import re
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRIEF = ROOT / "CLAUDE.md"
WORKSPACE = ROOT / "pyproject.toml"
CI = ROOT / ".github/workflows/ci.yml"
DOCS = ("CLAUDE.md", "README.md", "README.ru.md", "DESIGN.md")


def backticked(text: str, suffix: str) -> set[str]:
    return set(re.findall(rf"`([^`\s]+{re.escape(suffix)})`", text))


def modules_of(package: Path) -> set[str]:
    """Every module a reader would expect to be named, so not `__init__.py`."""
    return {path.name for path in package.glob("*.py")} - {"__init__.py"}


def bare_command(run: str) -> str:
    """A CI step's command without its flags: `uv run pytest -q --cov` → `uv run pytest`."""
    kept = [token for token in run.split() if not token.startswith("-") and token != "."]
    return " ".join(kept)


class TestTheAgentBrief:
    """CLAUDE.md is loaded automatically and acted on, so it is pinned hardest."""

    def test_it_names_exactly_the_modules_that_stay_in_ddiver(self):
        """The one fact a reader needs first: what still needs the user session.

        Both directions on purpose. A module that moved to the core and is still listed here
        sends somebody to the wrong package; one that arrived and is missing hides it.
        """
        row = next(
            line
            for line in BRIEF.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `ddiver/`")
        )
        assert backticked(row, ".py") == modules_of(ROOT / "ddiver")

    def test_it_names_every_workspace_member(self):
        members = re.search(r"members = \[(.*?)\]", WORKSPACE.read_text(encoding="utf-8"))
        assert members is not None
        brief = BRIEF.read_text(encoding="utf-8")
        for member in re.findall(r'"([^"]+)"', members.group(1)):
            assert member in brief, f"{member} is a workspace member CLAUDE.md does not mention"

    def test_the_gate_it_documents_is_the_one_ci_runs(self):
        """A brief that teaches a weaker gate than CI enforces wastes a round trip per change."""
        brief = BRIEF.read_text(encoding="utf-8")
        for run in re.findall(r"^\s*run: (.+)$", CI.read_text(encoding="utf-8"), re.MULTILINE):
            if not (run.startswith(("uv run", "shellcheck"))):
                continue
            for step in run.split(" && "):
                assert bare_command(step) in brief, f"CI runs `{step}`; CLAUDE.md does not say so"

    def test_it_says_to_run_mypy_over_everything(self):
        """`uv run mypy ddiver` has hidden real errors here, which is why CI passes no path."""
        assert re.search(r"^uv run mypy$", BRIEF.read_text(encoding="utf-8"), re.MULTILINE)


@pytest.mark.parametrize("doc", DOCS)
class TestEveryDocument:
    def test_every_module_it_names_exists(self, doc):
        """Catches the rename nobody propagated, in any of the four documents."""
        text = (ROOT / doc).read_text(encoding="utf-8")
        packages = sorted((ROOT / "packages").glob("*/src"))
        installed = Path(sysconfig.get_paths()["purelib"])
        # People write a module from wherever they are standing: from the repository root
        # (`tests/test_packaging.py`), from inside the workspace (`ddiver_core/app.py`), or from
        # inside the package itself (`bot/router.py`). All three are legitimate.
        # A citation may also point into a dependency ("verified in Telethon's own ..."), and
        # that file has to be real as well: a release that moves it should fail here rather than
        # leave the claim standing.
        bases = [
            ROOT,
            ROOT / "ddiver",
            installed,
            *packages,
            *(p_ for src in packages for p_ in src.glob("*")),
        ]
        for name in backticked(text, ".py"):
            if any((base / name).exists() for base in bases):
                continue
            loose = "/" not in name and any(base.rglob(name) for base in bases)
            assert loose, f"{doc} names `{name}`, and no such module exists"

    def test_every_path_it_links_to_exists(self, doc):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#:]+)\)", text):
            assert (ROOT / target).exists(), f"{doc} links to {target}, which is not there"
