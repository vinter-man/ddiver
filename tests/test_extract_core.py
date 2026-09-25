"""The extraction script, on small sources rather than on ddiver itself.

Its rewrites are the part that can break a suite silently in both directions: one that misses a
patch string leaves a test patching a module nobody reads any more, and one that rewrites too
much turns the export format tag ``"ddiver.config"`` into a module path. The refusals matter as
much: each one stands for a case where a mechanical rewrite would have been wrong.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import tomllib
from pathlib import Path

import pytest
from tools import extract_core as ec
from tools.extract_core import (
    CORE_SRC,
    TESTING,
    TESTING_ROOT,
    TESTING_SRC,
    BaseSplit,
    ExtractError,
    Relocations,
    Result,
    Split,
)

RELOC = Relocations(
    modules={
        "ddiver.delivery": "ddiver_core.delivery",
        "ddiver.bot": "ddiver_core.bot",
        "ddiver.portable": "ddiver_core.portable",
        "ddiver.config": "ddiver_core.config",
    },
    symbols={
        ("ddiver.runtime", "BotAdapter"): "ddiver_core.botruntime",
        ("ddiver.runtime", "_parse_mode"): "ddiver_core.botruntime",
        ("ddiver.clients", "make_bot_client"): "ddiver_core.botclient",
        ("tests.fakes", "T0"): "ddiver_core_testing",
    },
    kept=frozenset({("ddiver.clients", "make_bot_client")}),
    bound=frozenset({("ddiver.runtime", "BotAdapter")}),
)


def rewrite(source: str) -> str:
    return ec.rewrite_source(source, RELOC)[0]


class TestImports:
    def test_a_moved_module_is_renamed_in_place_keeping_layout_and_comments(self):
        source = "from ddiver.delivery import (\n    QUEUE_MAXSIZE,  # the cap\n    Delivery,\n)\n"
        assert rewrite(source) == source.replace(
            "from ddiver.delivery", "from ddiver_core.delivery"
        )

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("from ddiver import portable\n", "from ddiver_core import portable\n"),
            ("from ddiver.bot import menus\n", "from ddiver_core.bot import menus\n"),
            ("from ddiver.bot.router import Edit\n", "from ddiver_core.bot.router import Edit\n"),
            (
                "import ddiver.delivery as delivery_module\n",
                "import ddiver_core.delivery as delivery_module\n",
            ),
            (
                "import os, ddiver.delivery as d  # why\n",
                "import os, ddiver_core.delivery as d  # why\n",
            ),
        ],
    )
    def test_modules_and_packages_follow_their_move(self, before, after):
        assert rewrite(before) == after

    def test_an_import_from_a_split_module_is_split_where_the_names_went(self):
        source = (
            "def f():\n"
            "    from ddiver.runtime import RECONCILE_SECONDS, BotAdapter, refresh_dialogs\n"
            "    return BotAdapter\n"
        )
        assert rewrite(source) == (
            "def f():\n"
            "    from ddiver.runtime import RECONCILE_SECONDS, refresh_dialogs\n"
            "    from ddiver_core.botruntime import BotAdapter\n"
            "    return BotAdapter\n"
        )

    def test_a_whole_import_from_a_split_module_just_changes_module(self):
        assert rewrite("from tests.fakes import T0\n") == "from ddiver_core_testing import T0\n"

    @pytest.mark.parametrize(
        "source",
        [
            "from ddiver.clients import make_bot_client, make_user_client\n",
            "from ddiver.listener import Listener\nimport os\n",
            "import ddiver.runtime as runtime_module\n",
            "from ddiver import clients\n",
        ],
    )
    def test_what_did_not_move_is_left_alone(self, source):
        assert rewrite(source) == source

    @pytest.mark.parametrize(
        ("source", "reason"),
        [
            ("import ddiver.delivery\n", "without an alias"),
            (
                "from ddiver.runtime import (\n    BotAdapter,  # x\n    Application,\n)\n",
                "comment",
            ),
            ("from .delivery import Delivery\n", "relative import"),
        ],
    )
    def test_what_a_rewrite_alone_would_get_wrong_is_refused(self, source, reason):
        with pytest.raises(ExtractError, match=reason):
            rewrite(source)


class TestPatchStrings:
    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ('"ddiver.delivery.QUEUE_MAXSIZE"', '"ddiver_core.delivery.QUEUE_MAXSIZE"'),
            ("'ddiver.bot.handlers.MAX_LINES'", "'ddiver_core.bot.handlers.MAX_LINES'"),
            ('"ddiver.config.Settings.load"', '"ddiver_core.config.Settings.load"'),
        ],
    )
    def test_a_patch_of_a_moved_module_follows_it(self, before, after):
        assert (
            rewrite(f"monkeypatch.setattr({before}, 3)\n") == f"monkeypatch.setattr({after}, 3)\n"
        )

    @pytest.mark.parametrize(
        "literal",
        [
            '"ddiver.config"',  # the export format tag, and also a module that moved
            '"ddiver.json"',
            '"ddiver.lock"',
            '"not ddiver.delivery.QUEUE_MAXSIZE"',
            '"ddiver.listener.CURSOR_FLUSH_SECONDS"',
        ],
    )
    def test_strings_that_are_not_patches_of_moved_names_stay(self, literal):
        source = f"x = {literal}\n"
        assert rewrite(source) == source

    def test_a_split_name_the_old_module_still_imports_is_patched_there(self):
        """Application reads runtime's own BotAdapter; a patch of the new home would miss it."""
        source = 'patch("ddiver.runtime.BotAdapter")\npatch("ddiver.clients.make_bot_client")\n'
        assert rewrite(source) == source

    def test_a_split_name_the_old_module_no_longer_has_follows_it(self):
        source = 'patch("ddiver.runtime._parse_mode")\n'
        assert rewrite(source) == 'patch("ddiver_core.botruntime._parse_mode")\n'

    def test_offsets_survive_cyrillic_on_the_same_line(self):
        """ast columns are UTF-8 bytes; slicing the str by them would cut into the wrong place."""
        source = 'x = ("Чат ёжик", "ddiver.delivery.QUEUE_MAXSIZE", "дальше")\n'
        assert (
            rewrite(source) == 'x = ("Чат ёжик", "ddiver_core.delivery.QUEUE_MAXSIZE", "дальше")\n'
        )

    def test_every_string_rewrite_is_reported(self):
        _, notes = ec.rewrite_source('setattr("ddiver.delivery.X", 1)\n', RELOC, path="t.py")
        assert notes == ['t.py:1: "ddiver_core.delivery.X"']


class TestReferences:
    def test_cross_references_point_at_the_definition(self):
        source = (
            '"""See :class:`~ddiver.bot.router.Router`, :func:`ddiver.runtime.BotAdapter`\n'
            'and :mod:`ddiver.listener`."""\n'
        )
        assert rewrite(source) == (
            '"""See :class:`~ddiver_core.bot.router.Router`, '
            ":func:`ddiver_core.botruntime.BotAdapter`\n"
            'and :mod:`ddiver.listener`."""\n'
        )

    def test_a_second_rewrite_changes_nothing(self):
        source = (
            '"""Uses :mod:`ddiver.delivery`."""\n'
            "from ddiver import portable\n"
            "from ddiver.runtime import Application, BotAdapter\n"
            'patch("ddiver.delivery.QUEUE_MAXSIZE")\n'
        )
        once = rewrite(source)
        assert once != source
        assert rewrite(once) == once


SOURCE = '''"""Old docstring."""

from __future__ import annotations

import logging
import os

from ddiver.config import Settings

log = logging.getLogger(__name__)

LIMIT = 3
OTHER = 4


# Why the adapter exists.
@decorate
class Adapter:
    def go(self) -> int:
        log.info("go")
        return LIMIT + OTHER


def helper(settings: Settings) -> str:
    return os.sep


def stays() -> Adapter:
    return Adapter()
'''

SPLIT = Split(
    source="ddiver/thing.py",
    target="thing",
    names=("LIMIT", "OTHER", "Adapter", "helper"),
    docstring="New module.",
    kept=("helper",),
    kept_reason="Tests patch it here.",
    source_docstring="What is left.",
)


class TestSplit:
    def test_the_new_module_carries_the_code_its_imports_and_its_logger(self):
        module = ec.split_source(SOURCE, SPLIT, path="ddiver/thing.py").module_text
        ast.parse(module)
        assert module.startswith('"""New module."""\n\nfrom __future__ import annotations\n')
        assert "from ddiver.config import Settings" in module  # rewritten afterwards
        assert (
            "from ddiver.config import Settings\n\n"
            "log = logging.getLogger(__name__)\n\n"
            "LIMIT = 3\nOTHER = 4\n\n\n"
            "# Why the adapter exists.\n@decorate\nclass Adapter:"
        ) in module
        assert "def helper(settings: Settings)" in module
        assert "def stays" not in module

    def test_what_is_left_imports_back_what_it_uses_and_what_is_kept(self):
        result = ec.split_source(SOURCE, SPLIT, path="ddiver/thing.py")
        text = result.source_text
        ast.parse(text)
        assert result.imported_back == ("Adapter", "helper")
        assert text.startswith('"""What is left."""\n')
        assert "from ddiver_core.thing import Adapter\n" in text
        assert (
            "# Tests patch it here.\n"
            "from ddiver_core.thing import helper as helper  # noqa: PLC0414\n"
        ) in text
        assert "class Adapter" not in text and "LIMIT = 3" not in text
        assert "def stays() -> Adapter:" in text

    def test_the_new_module_starts_with_only_the_imports_its_code_uses(self):
        """ruff will not drop an unused import from an ``__init__.py``: it may be a re-export."""
        source = (
            '"""Doc."""\nfrom __future__ import annotations\n\nimport os, sys\n'
            "import telethon.tl as tl\nfrom pathlib import Path, PurePath as Pure\n\n"
            "def f() -> Pure:\n    return Pure(os.sep)\n\ng = sys.argv\n"
        )
        split = Split(
            source="tests/fakes.py", target="", names=("f",), docstring="D.", package=TESTING
        )
        module = ec.split_source(source, split, path="tests/fakes.py").module_text
        assert module == (
            '"""D."""\n\nfrom __future__ import annotations\nimport os\n'
            "from pathlib import PurePath as Pure\n\n\n"
            "def f() -> Pure:\n    return Pure(os.sep)\n"
        )

    def test_moved_code_that_needs_something_left_behind_is_refused(self):
        split = Split(source="ddiver/thing.py", target="thing", names=("Adapter",), docstring="")
        with pytest.raises(ExtractError, match="LIMIT, OTHER"):
            ec.split_source(SOURCE, split, path="ddiver/thing.py")

    def test_a_name_that_no_longer_exists_is_refused(self):
        split = Split(source="ddiver/thing.py", target="thing", names=("Gone",), docstring="")
        with pytest.raises(ExtractError, match="Gone not defined"):
            ec.split_source(SOURCE, split, path="ddiver/thing.py")

    def test_conditional_imports_are_refused(self):
        source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import os\nA = 1\n"
        split = Split(source="ddiver/x.py", target="x", names=("A",), docstring="")
        with pytest.raises(ExtractError, match="conditional imports"):
            ec.split_source(source, split, path="ddiver/x.py")

    def test_a_source_without_a_docstring_cannot_have_it_replaced(self):
        split = Split(
            source="ddiver/x.py", target="x", names=("A",), docstring="", source_docstring=""
        )
        with pytest.raises(ExtractError, match="no module docstring"):
            ec.split_source("A = 1\nB = A\n", split, path="ddiver/x.py")

    def test_a_split_that_leaves_nothing_behind_to_import(self):
        split = Split(source="ddiver/x.py", target="x", names=("A",), docstring="Doc.")
        result = ec.split_source('"""Doc."""\nimport os\n\nA = os.sep\nB = 2\n', split, path="x")
        assert result.imported_back == ()
        assert "ddiver_core" not in result.source_text


class TestConfiguration:
    PYPROJECT = (
        "[tool.ruff.lint.per-file-ignores]\n"
        '"ddiver/app.py" = ["BLE001"]\n'
        '"ddiver/apps.py" = ["T201"]\n'
        '"ddiver/__main__.py" = ["PLC0415", "BLE001", "T201"]\n'
        '"ddiver/i18n/*.py" = ["E501"]\n'
        '"tests/**" = ["S101", "ARG"]\n'
        "\n[tool.ruff.lint.isort]\n"
    )

    def test_ignore_keys_follow_their_files_and_only_theirs(self):
        text = ec.move_ignore_keys(self.PYPROJECT, ["app", "i18n"])
        assert f'"{CORE_SRC}/app.py" = ["BLE001"]' in text
        assert f'"{CORE_SRC}/i18n/*.py" = ["E501"]' in text
        assert '"ddiver/apps.py"' in text
        assert '"ddiver/__main__.py"' in text

    def test_a_split_off_module_gets_only_the_ignores_it_still_needs(self, tmp_path):
        split = Split(
            source="ddiver/__main__.py",
            target="process",
            names=("X",),
            docstring="",
            ignores_reason="Because.",
        )
        run = FakeRunner(tmp_path, ruff_json=[{"code": "BLE001"}, {"code": "PLC0415"}])
        text = ec.derive_ignores(split, self.PYPROJECT, root=tmp_path, run=run)
        assert f'# Because.\n"{CORE_SRC}/process.py" = ["PLC0415", "BLE001"]\n\n[' in text

    def test_a_split_off_module_that_needs_nothing_gets_no_entry(self, tmp_path):
        split = Split(source="tests/fakes.py", target="testing", names=("X",), docstring="")
        text = ec.derive_ignores(split, self.PYPROJECT, root=tmp_path, run=FakeRunner(tmp_path))
        assert text == self.PYPROJECT

    def test_a_split_off_module_breaking_a_rule_its_source_could_not_is_refused(self, tmp_path):
        split = Split(source="tests/fakes.py", target="testing", names=("X",), docstring="")
        run = FakeRunner(tmp_path, ruff_json=[{"code": "S101"}, {"code": "E501"}])
        with pytest.raises(ExtractError, match="E501"):
            ec.derive_ignores(split, self.PYPROJECT, root=tmp_path, run=run)

    @pytest.mark.parametrize("anchor", ["missing", "\n"])
    def test_an_anchor_must_be_there_exactly_once(self, anchor):
        with pytest.raises(ExtractError, match="exactly once"):
            ec.replace_once("a\nb\nc\n", anchor, "x", what="f")


class FakeRunner:
    """git, ruff and uv, done in-process: `git mv` renames and ruff reports canned findings."""

    def __init__(
        self, root: Path, *, dirty: str = "", ruff_json: list[dict[str, str]] | None = None
    ):
        self.root = root
        self.dirty = dirty
        self.ruff_json = ruff_json or []
        self.calls: list[list[str]] = []

    def __call__(self, args, *, cwd):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["git", "mv"]:
            (cwd / args[2]).rename(cwd / args[3])
        elif args[:2] == ["git", "ls-files"]:
            files = sorted(
                p for p in (cwd / args[-1]).rglob("*.py") if "__pycache__" not in p.parts
            )
            return Result(0, "\n".join(p.relative_to(cwd).as_posix() for p in files))
        elif args[:2] == ["git", "status"]:
            return Result(0, self.dirty)
        elif "--output-format" in args:
            return Result(0, json.dumps(self.ruff_json))
        return Result(0, "")

    def added(self) -> list[str]:
        return [path for call in self.calls if call[:2] == ["git", "add"] for path in call[3:]]


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestModuleMoves:
    def test_a_package_moves_file_by_file_and_leaves_no_namespace_package(self, tmp_path):
        write(tmp_path, "ddiver/bot/__init__.py")
        write(tmp_path, "ddiver/bot/menus.py", "X = 1\n")
        write(tmp_path, "ddiver/bot/__pycache__/menus.cpython-312.pyc", "stale")
        moved = ec.move_module("bot", root=tmp_path, run=FakeRunner(tmp_path))
        assert moved == {f"{CORE_SRC}/bot/__init__.py", f"{CORE_SRC}/bot/menus.py"}
        assert (tmp_path / CORE_SRC / "bot/menus.py").read_text() == "X = 1\n"
        assert not (tmp_path / "ddiver/bot").exists()

    def test_an_untracked_file_left_in_a_moved_package_is_refused(self, tmp_path):
        write(tmp_path, "ddiver/bot/__init__.py")
        write(tmp_path, "ddiver/bot/notes.txt", "mine")
        with pytest.raises(ExtractError, match=r"untracked files left .*notes\.txt"):
            ec.move_module("bot", root=tmp_path, run=FakeRunner(tmp_path))

    def test_a_module_that_is_not_there_is_refused(self, tmp_path):
        with pytest.raises(ExtractError, match="neither a module nor a package"):
            ec.move_module("gone", root=tmp_path, run=FakeRunner(tmp_path))

    def test_a_target_that_already_exists_is_refused(self, tmp_path):
        write(tmp_path, "ddiver/links.py")
        write(tmp_path, f"{CORE_SRC}/links.py")
        with pytest.raises(ExtractError, match="already exists"):
            ec.move_module("links", root=tmp_path, run=FakeRunner(tmp_path))


def repo_after_leaves(root: Path) -> None:
    """The smallest tree on which the `storage` step can run: scaffold and leaves applied."""
    write(
        root, "pyproject.toml", '[tool.ruff.lint.per-file-ignores]\n"ddiver/storage.py" = ["S1"]\n'
    )
    write(root, f"{CORE_SRC}/__init__.py")
    write(root, f"{CORE_SRC}/timeutil.py", "def utcnow() -> int:\n    return 0\n")
    write(root, "ddiver/__init__.py")
    write(root, "ddiver/links.py", "class ChatRef:\n    pass\n")
    write(
        root,
        "ddiver/storage.py",
        '"""See :mod:`ddiver.links`."""\nfrom ddiver.links import ChatRef\n'
        "from ddiver_core.timeutil import utcnow\nQUEUE = 1\n",
    )
    write(root, "ddiver/listener.py", "from ddiver.storage import QUEUE\n")
    write(
        root,
        "tests/test_storage.py",
        "import ddiver.storage as storage_module\n"
        'PATCH = "ddiver.storage.QUEUE"\nFORMAT = "ddiver.config"\n',
    )


class TestApplyStep:
    def test_a_step_moves_rewrites_and_stages_what_it_touched(self, tmp_path):
        repo_after_leaves(tmp_path)
        run = FakeRunner(tmp_path)
        report = ec.apply_step("storage", root=tmp_path, run=run)

        assert not (tmp_path / "ddiver/storage.py").exists()
        storage = (tmp_path / CORE_SRC / "storage.py").read_text()
        assert storage.startswith('"""See :mod:`ddiver_core.links`."""\nfrom ddiver_core.links')
        assert (tmp_path / "ddiver/listener.py").read_text() == (
            "from ddiver_core.storage import QUEUE\n"
        )
        test = (tmp_path / "tests/test_storage.py").read_text()
        assert "import ddiver_core.storage as storage_module" in test
        assert 'PATCH = "ddiver_core.storage.QUEUE"' in test
        assert 'FORMAT = "ddiver.config"' in test
        assert f'"{CORE_SRC}/storage.py" = ["S1"]' in (tmp_path / "pyproject.toml").read_text()
        assert set(run.added()) == set(report.touched)
        assert {"ddiver/listener.py", "tests/test_storage.py", "pyproject.toml"} <= set(
            report.touched
        )
        assert report.notes == (
            f"{CORE_SRC}/storage.py: :mod:`ddiver.links` -> ddiver_core.links",
            'tests/test_storage.py:2: "ddiver_core.storage.QUEUE"',
        )

    def test_core_that_still_imports_the_app_is_refused(self, tmp_path):
        repo_after_leaves(tmp_path)
        write(tmp_path, "ddiver/links.py", "from ddiver.listener import QUEUE\n")
        with pytest.raises(ExtractError, match="core must not"):
            ec.apply_step("storage", root=tmp_path, run=FakeRunner(tmp_path))

    def test_steps_run_in_order(self, tmp_path):
        with pytest.raises(ExtractError, match="apply 'scaffold' first"):
            ec.apply_step("storage", root=tmp_path, run=FakeRunner(tmp_path))

    def test_a_step_runs_once(self, tmp_path):
        repo_after_leaves(tmp_path)
        with pytest.raises(ExtractError, match="already applied"):
            ec.apply_step("leaves", root=tmp_path, run=FakeRunner(tmp_path))

    def test_a_dirty_tree_is_refused_unless_asked(self, tmp_path):
        repo_after_leaves(tmp_path)
        with pytest.raises(ExtractError, match="uncommitted changes"):
            ec.apply_step("storage", root=tmp_path, run=FakeRunner(tmp_path, dirty=" M x.py\n"))
        ec.apply_step(
            "storage", root=tmp_path, run=FakeRunner(tmp_path, dirty=" M x.py\n"), allow_dirty=True
        )

    def test_an_unknown_step_is_named(self):
        with pytest.raises(ExtractError, match="no step 'nope'"):
            ec.step_named("nope")


class TestCommandLine:
    def test_status_lists_every_step(self, tmp_path, capsys):
        repo_after_leaves(tmp_path)
        assert ec.main(["status"], root=tmp_path, run=FakeRunner(tmp_path)) == 0
        out = capsys.readouterr().out.splitlines()
        assert [line.split()[:2] for line in out] == [
            ["applied", "scaffold"],
            ["applied", "leaves"],
            ["pending", "storage"],
            ["pending", "bot"],
            ["pending", "clients"],
            ["pending", "runtime"],
        ]

    def test_apply_prints_the_commit_subject(self, tmp_path, capsys):
        repo_after_leaves(tmp_path)
        assert ec.main(["apply", "storage"], root=tmp_path, run=FakeRunner(tmp_path)) == 0
        assert "Move links and storage into ddiver-core" in capsys.readouterr().out

    def test_a_refusal_is_an_exit_code_and_a_message(self, tmp_path, capsys):
        repo_after_leaves(tmp_path)
        assert ec.main(["apply", "bot"], root=tmp_path, run=FakeRunner(tmp_path)) == 1
        assert "apply 'storage' first" in capsys.readouterr().err

    def test_it_must_run_from_the_repository_root(self, tmp_path, capsys):
        assert ec.main(["status"], root=tmp_path, run=FakeRunner(tmp_path)) == 2
        assert "root of the ddiver repository" in capsys.readouterr().err

    def test_check_stops_at_the_first_failure(self, tmp_path):
        repo_after_leaves(tmp_path)
        calls: list[list[str]] = []

        def run(args, *, cwd):
            calls.append(list(args))
            return Result(1 if "mypy" in args else 0, "")

        assert ec.main(["check"], root=tmp_path, run=run) == 1
        assert [call[2:] for call in calls] == [
            ["ruff", "check", "."],
            ["ruff", "format", "--check", "."],
            ["mypy"],
        ]


class TestBookkeeping:
    @pytest.mark.parametrize(
        ("path", "module"),
        [
            ("ddiver/bot/__init__.py", "ddiver.bot"),
            ("ddiver/__main__.py", "ddiver.__main__"),
            ("tests/fakes.py", "tests.fakes"),
        ],
    )
    def test_a_file_path_names_its_module(self, path, module):
        assert ec.module_of(path) == module

    def test_a_failed_command_is_reported_with_its_output(self, tmp_path):
        def run(args, *, cwd):
            return Result(3, "said this", "and this")

        with pytest.raises(ExtractError, match=r"`git mv a b` exited 3:\nsaid thisand this"):
            ec.must(run, ["git", "mv", "a", "b"], cwd=tmp_path)

    def test_a_split_module_is_known_to_still_import_what_it_uses(self, tmp_path):
        """What decides whether `"ddiver.runtime.X"` stays a patch of runtime."""
        write(tmp_path, "ddiver/runtime.py", "from ddiver_core.botruntime import BotAdapter\n")
        index, _ = ec.step_named("runtime")
        reloc = ec.relocations_through(index, tmp_path)
        assert ("ddiver.runtime", "BotAdapter") in reloc.bound
        assert ("ddiver.runtime", "_parse_mode") not in reloc.bound
        assert reloc.symbols["ddiver.runtime", "_parse_mode"] == "ddiver_core.botruntime"
        assert reloc.symbols["tests.fakes", "FakeClock"] == TESTING
        assert reloc.kept == {("ddiver.clients", "make_bot_client")}
        assert reloc.bases == {("ddiver.config", "Settings"): "ddiver_core.config"}
        assert ("ddiver.config", "Settings") not in reloc.symbols
        assert reloc.module("ddiver.bot.handlers") == "ddiver_core.bot.handlers"
        assert reloc.module("ddiver.listener") is None


PYPROJECT_BEFORE_SCAFFOLD = """\
[project]
name = "ddiver"
dependencies = [
  "telethon",
]

[dependency-groups]
dev = [
  "pytest",
]

[build-system]
requires = ["hatchling"]

[tool.ruff]
src = ["ddiver", "tests"]

[tool.ruff.lint.isort]
known-first-party = ["ddiver"]

[tool.mypy]
files = ["ddiver", "tests"]

[tool.coverage.run]
source = ["ddiver"]
"""


def repo_before_scaffold(root: Path) -> None:
    """Exactly the anchors the scaffold edits, and nothing else."""
    write(root, "pyproject.toml", PYPROJECT_BEFORE_SCAFFOLD)
    write(root, "Dockerfile", f"FROM python\n{ec.DOCKERFILE_OLD}")
    write(root, ".dockerignore", "tests/\nscripts/\n")
    write(root, ".github/workflows/ci.yml", "jobs:\n  check:\n  deploy:\n    needs: check\n")
    write(root, "README.md", "```\nuv sync                      # or: pip install -e .\n```\n")
    write(root, "README.ru.md", "```\nuv sync                      # или: pip install -e .\n```\n")
    write(root, "ddiver/__init__.py")


class TestScaffold:
    def test_it_makes_the_workspace_and_the_build_aware_of_it(self, tmp_path):
        repo_before_scaffold(tmp_path)
        run = FakeRunner(tmp_path)
        touched = ec.scaffold(tmp_path, run)

        assert (tmp_path / CORE_SRC / "py.typed").read_text() == ""
        assert (
            "Nothing in here imports ``ddiver``"
            in (tmp_path / CORE_SRC / "__init__.py").read_text()
        )
        pyproject = (tmp_path / "pyproject.toml").read_text()
        assert "ddiver-core = { workspace = true }" in pyproject
        assert 'source = ["ddiver", "ddiver_core"]' in pyproject
        dockerfile = (tmp_path / "Dockerfile").read_text()
        assert "--no-install-workspace" in dockerfile and "--no-editable" in dockerfile
        assert (tmp_path / ".dockerignore").read_text() == "tests/\nscripts/\ntools/\n"
        ci = (tmp_path / ".github/workflows/ci.yml").read_text()
        assert "  image:\n" in ci and "    needs: [check, image]\n" in ci
        assert "--network none" in ci and "walk_packages" in ci
        assert "pip install -e ./packages/ddiver-core -e ." in (tmp_path / "README.md").read_text()
        assert (
            "pip install -e ./packages/ddiver-core -e ." in (tmp_path / "README.ru.md").read_text()
        )
        assert [call[1:] for call in run.calls] == [["lock"], ["sync"]]
        assert "uv.lock" in touched  # written by `uv lock`, which the fake does not run
        assert all((tmp_path / path).exists() for path in touched - {"uv.lock"})

    def test_a_dockerfile_that_moved_on_is_refused_rather_than_guessed(self, tmp_path):
        repo_before_scaffold(tmp_path)
        write(tmp_path, "Dockerfile", "FROM python\nCOPY . .\n")
        with pytest.raises(ExtractError, match="Dockerfile: expected this exactly once"):
            ec.scaffold(tmp_path, FakeRunner(tmp_path))

    def test_an_edit_that_breaks_the_toml_is_refused(self, tmp_path):
        """Upstream already has a workspace table: a second one is a duplicate, not a merge."""
        repo_before_scaffold(tmp_path)
        existing = PYPROJECT_BEFORE_SCAFFOLD + '\n[tool.uv.workspace]\nmembers = ["x"]\n'
        write(tmp_path, "pyproject.toml", existing)
        with pytest.raises(ExtractError, match="not valid TOML"):
            ec.scaffold(tmp_path, FakeRunner(tmp_path))

    def test_it_runs_once(self, tmp_path):
        repo_before_scaffold(tmp_path)
        write(tmp_path, f"{CORE_SRC}/__init__.py")
        with pytest.raises(ExtractError, match="already exists"):
            ec.scaffold(tmp_path, FakeRunner(tmp_path))


CONFIG = '''"""Settings, from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

LIMIT = 3


class Settings(BaseSettings):
    token: str = Field(description="shared")
    data_dir: Path = Path("data")

    # Only the app has a session.
    hours: int = 24

    @field_validator("token")
    @classmethod
    def _token(cls, value: str) -> str:
        return value

    @classmethod
    def load(cls) -> Settings:
        return cls()

    @property
    def session(self) -> Path:
        return self.data_dir / "user.session"
'''

BASE = BaseSplit(
    source="ddiver/config.py",
    target="config",
    cls="Settings",
    names=("token", "data_dir", "_token", "load"),
    stays=("hours", "session"),
    alias="CoreSettings",
    docstring="Shared settings.",
    source_docstring="The app's settings.",
)


def split_config(source: str = CONFIG, spec: BaseSplit = BASE) -> ec.SplitResult:
    return ec.split_class(source, spec, path="ddiver/config.py")


class TestClassSplit:
    def test_the_base_carries_the_shared_members_under_the_original_header(self):
        module = split_config().module_text
        ast.parse(module)
        assert module.startswith('"""Shared settings."""\n\nfrom __future__ import annotations\n')
        assert "\nfrom typing import Self\n" in module
        assert (
            "class Settings(BaseSettings):\n"
            '    token: str = Field(description="shared")\n'
            '    data_dir: Path = Path("data")\n\n'
            '    @field_validator("token")\n'
        ) in module
        assert "    def load(cls) -> Self:\n" in module
        assert "hours" not in module and "def session" not in module
        assert "LIMIT" not in module

    def test_the_app_keeps_a_subclass_of_the_same_name_with_the_rest(self):
        result = split_config()
        text = result.source_text
        ast.parse(text)
        assert result.imported_back == ("Settings",)
        assert text.startswith('"""The app\'s settings."""\n')
        assert "from ddiver_core.config import Settings as CoreSettings\n" in text
        assert text.endswith(
            "class Settings(CoreSettings):\n"
            "    # Only the app has a session.\n"
            "    hours: int = 24\n\n"
            "    @property\n"
            "    def session(self) -> Path:\n"
            '        return self.data_dir / "user.session"\n'
        )
        assert "LIMIT = 3" in text
        assert "token: str" not in text and "def load" not in text

    def test_a_self_the_source_already_imports_is_not_imported_twice(self):
        source = CONFIG.replace(
            "from pathlib import Path\n", "from pathlib import Path\nfrom typing import Self\n"
        )
        assert split_config(source).module_text.count("import Self") == 1

    def test_a_class_docstring_stays_with_the_subclass(self):
        source = CONFIG.replace(
            "class Settings(BaseSettings):\n",
            'class Settings(BaseSettings):\n    """Knobs."""\n\n',
        )
        result = split_config(source)
        assert '"""Knobs."""' not in result.module_text
        assert 'class Settings(CoreSettings):\n    """Knobs."""\n\n    # Only' in result.source_text

    def test_a_member_whose_group_opener_went_elsewhere_still_opens_a_group(self):
        source = (
            "class Settings(BaseSettings):\n"
            "    a: int = 1\n"
            "    b: int = 2\n"
            "\n"
            "    c: int = 3\n"
            "    d: int = 4\n"
        )
        spec = dataclasses.replace(BASE, names=("a", "d"), stays=("b", "c"))
        result = ec.split_class('"""Doc."""\n' + source, spec, path="ddiver/config.py")
        assert result.module_text.endswith(
            "class Settings(BaseSettings):\n    a: int = 1\n\n    d: int = 4\n"
        )
        assert result.source_text.endswith(
            "class Settings(CoreSettings):\n    b: int = 2\n\n    c: int = 3\n"
        )

    def test_a_member_nobody_classified_is_refused(self):
        source = CONFIG.replace(
            "    hours: int = 24\n", "    hours: int = 24\n    extra: int = 1\n"
        )
        with pytest.raises(ExtractError, match="extra, which is neither carried"):
            split_config(source)

    def test_a_classified_member_that_is_gone_is_refused(self):
        spec = dataclasses.replace(BASE, stays=(*BASE.stays, "gone"))
        with pytest.raises(ExtractError, match="gone not in Settings"):
            split_config(spec=spec)

    def test_a_carried_member_reading_one_that_stays_is_refused(self):
        spec = dataclasses.replace(
            BASE, names=("token", "_token", "load", "session"), stays=("data_dir", "hours")
        )
        with pytest.raises(ExtractError, match="session reads data_dir"):
            split_config(spec=spec)

    def test_a_carried_validator_of_a_field_that_stays_is_refused(self):
        spec = dataclasses.replace(
            BASE, names=("data_dir", "_token", "load", "session"), stays=("token", "hours")
        )
        with pytest.raises(ExtractError, match="_token is declared for token"):
            split_config(spec=spec)

    def test_carried_code_that_needs_a_module_level_name_is_refused(self):
        source = CONFIG.replace(
            '    data_dir: Path = Path("data")\n', "    data_dir: int = LIMIT\n"
        )
        with pytest.raises(ExtractError, match="data_dir uses LIMIT"):
            split_config(source)

    @pytest.mark.parametrize(
        ("before", "after", "reason"),
        [
            ("class Settings(BaseSettings):\n", "class Settings(\n    BaseSettings\n):\n", "spans"),
            ("class Settings(BaseSettings):\n", "@final\nclass Settings(BaseSettings):\n", "decor"),
            ("class Settings(BaseSettings):\n", "class Other:\n    pass\n\n\n", "one top-level"),
            ("    hours: int = 24\n", "    hours: int = 24\n    pass\n", "without one name"),
        ],
    )
    def test_what_a_mechanical_class_split_would_get_wrong_is_refused(self, before, after, reason):
        with pytest.raises(ExtractError, match=reason):
            split_config(CONFIG.replace(before, after))

    def test_a_split_that_leaves_the_subclass_empty_is_refused(self):
        spec = dataclasses.replace(BASE, names=(*BASE.names, *BASE.stays), stays=())
        with pytest.raises(ExtractError, match="nothing stays"):
            split_config(spec=spec)


class TestPackagesReadTheBase:
    RELOC = Relocations(bases={("ddiver.config", "Settings"): "ddiver_core.config"})

    def test_code_in_the_packages_reads_the_base_and_the_app_its_own_class(self):
        source = 'from ddiver.config import Settings\n"""See :class:`ddiver.config.Settings`."""\n'
        core = ec.view_for(f"{CORE_SRC}/botclient.py", self.RELOC)
        assert ec.rewrite_source(source, core)[0] == (
            "from ddiver_core.config import Settings\n"
            '"""See :class:`ddiver_core.config.Settings`."""\n'
        )
        app = ec.view_for("ddiver/runtime.py", self.RELOC)
        assert ec.rewrite_source(source, app)[0] == source

    def test_a_patch_string_of_the_app_class_is_left_alone(self):
        source = 'patch("ddiver.config.Settings.load")\n'
        assert ec.rewrite_source(source, self.RELOC)[0] == source


class TestSplitIntoAPackage:
    def test_an_empty_target_is_the_package_itself(self):
        split = Split(
            source="tests/fakes.py", target="", names=("A",), docstring="", package=TESTING
        )
        assert split.target_module == TESTING
        assert split.target_path == f"{TESTING_SRC}/__init__.py"
        assert ec.FAKES.target_module == TESTING

    def test_the_fakes_are_split_into_the_testing_package(self, tmp_path):
        write(tmp_path, "tests/fakes.py", '"""Fakes."""\nimport os\n\nT0 = os.sep\nB = T0\n')
        split = Split(
            source="tests/fakes.py", target="", names=("T0",), docstring="Doc.", package=TESTING
        )
        assert ec.apply_split(split, tmp_path) == {"tests/fakes.py", f"{TESTING_SRC}/__init__.py"}
        assert (tmp_path / TESTING_SRC / "__init__.py").read_text().startswith('"""Doc."""\n')
        assert "from ddiver_core_testing import T0\n" in (tmp_path / "tests/fakes.py").read_text()


class TestPackageBoundaries:
    @pytest.mark.parametrize(
        ("rel", "text", "reason"),
        [
            (f"{CORE_SRC}/delivery.py", "from ddiver_core_testing import T0\n", "ddiver_core must"),
            (f"{CORE_SRC}/delivery.py", "import tests.fakes as f\n", "ddiver_core must not"),
            (f"{TESTING_SRC}/__init__.py", "from tests.fakes import make_event\n", "testing must"),
            (f"{TESTING_SRC}/__init__.py", "from ddiver.runtime import X\n", "testing must not"),
        ],
    )
    def test_what_a_package_imports_is_checked(self, tmp_path, rel, text, reason):
        write(tmp_path, rel, text)
        with pytest.raises(ExtractError, match=reason):
            ec.verify(tmp_path, Relocations())

    def test_the_fakes_may_use_the_core(self, tmp_path):
        write(tmp_path, f"{TESTING_SRC}/__init__.py", "from ddiver_core.timeutil import utcnow\n")
        ec.verify(tmp_path, Relocations())


def repo_after_scaffold(root: Path) -> None:
    repo_before_scaffold(root)
    ec.scaffold(root, FakeRunner(root))


class TestMember:
    def test_a_member_is_written_and_the_build_and_ci_are_told(self, tmp_path):
        repo_after_scaffold(tmp_path)
        touched = ec.add_member(ec.TESTING_MEMBER, tmp_path)

        assert (tmp_path / TESTING_SRC / "py.typed").read_text() == ""
        member = (tmp_path / TESTING_ROOT / "pyproject.toml").read_text()
        assert 'name = "ddiver-core-testing"' in member and "dependencies = []" in member
        pyproject = tomllib.loads((tmp_path / "pyproject.toml").read_text())
        assert pyproject["tool"]["uv"]["workspace"]["members"] == [
            "packages/ddiver-core",
            TESTING_ROOT,
        ]
        assert pyproject["tool"]["uv"]["sources"]["ddiver-core-testing"] == {"workspace": True}
        assert "ddiver-core-testing" in pyproject["dependency-groups"]["dev"]
        assert "ddiver-core-testing" not in pyproject["project"]["dependencies"]
        assert TESTING in pyproject["tool"]["ruff"]["lint"]["isort"]["known-first-party"]
        assert f"{TESTING_ROOT}/src" in pyproject["tool"]["mypy"]["files"]
        assert TESTING not in pyproject["tool"]["coverage"]["run"]["source"]
        dockerfile = (tmp_path / "Dockerfile").read_text()
        assert f"COPY {TESTING_ROOT}/pyproject.toml ./{TESTING_ROOT}/\n" in dockerfile
        assert f"{TESTING_ROOT}/src/\n" in (tmp_path / ".dockerignore").read_text()
        ci = (tmp_path / ".github/workflows/ci.yml").read_text()
        assert 'dev_only = ("pytest", "ddiver_core_testing")\n' in ci
        assert {
            "pyproject.toml",
            "Dockerfile",
            ".dockerignore",
            f"{TESTING_SRC}/py.typed",
        } <= touched

    def test_a_member_that_is_there_already_is_refused(self, tmp_path):
        repo_after_scaffold(tmp_path)
        write(tmp_path, f"{TESTING_ROOT}/pyproject.toml")
        with pytest.raises(ExtractError, match="already exists"):
            ec.add_member(ec.TESTING_MEMBER, tmp_path)

    def test_a_build_that_moved_on_is_refused_rather_than_guessed(self, tmp_path):
        repo_after_scaffold(tmp_path)
        write(tmp_path, ".dockerignore", "tests/\n")
        with pytest.raises(ExtractError, match=r"\.dockerignore: expected this exactly once"):
            ec.add_member(ec.TESTING_MEMBER, tmp_path)


def settings_source() -> str:
    """A class with exactly the members the real split classifies, in that order."""
    members = []
    for name in (*ec.SETTINGS.names, *ec.SETTINGS.stays):
        if name == "model_config":
            members.append("    model_config = {}\n")
        elif name == "load":
            members.append(
                "    @classmethod\n    def load(cls) -> Settings:\n        return cls()\n"
            )
        elif name.startswith("_"):
            members.append(
                f'    @field_validator("bot_token")\n    @classmethod\n'
                f"    def {name}(cls, value: str) -> str:\n        return value\n"
            )
        elif name in {"bot_session", "db_path", "heartbeat_path", "regex_timeout", "bot_id"} or (
            name == "user_session"
        ):
            members.append(f"    @property\n    def {name}(self) -> str:\n        return ''\n")
        else:
            members.append(f"    {name}: int = 0\n")
    return (
        '"""Settings."""\n\nfrom pydantic_settings import BaseSettings\n\n\n'
        "class Settings(BaseSettings):\n" + "\n".join(members)
    )


CLIENTS = '''"""Clients."""

from ddiver.config import Settings

CONNECT_TIMEOUT = 30.0
BOT_FLOOD_THRESHOLD = 0


def is_fatal_auth_error(exc: BaseException) -> bool:
    return False


def is_session_file_error(exc: BaseException) -> bool:
    return False


async def connect_with_timeout(client: object, timeout: float = CONNECT_TIMEOUT) -> None:
    return None


def make_bot_client(settings: Settings) -> object:
    return (settings.bot_session, BOT_FLOOD_THRESHOLD)


def make_user_client(settings: Settings) -> object:
    return settings.user_session
'''


def repo_after_bot(root: Path) -> None:
    """The smallest tree the `clients` step runs on: every earlier step applied."""
    repo_after_scaffold(root)
    for done in ("timeutil", "links", "matcher"):
        write(root, f"{CORE_SRC}/{done}.py")
    write(
        root, "ddiver/app.py", '"""Supervisor."""\nfrom ddiver.clients import is_fatal_auth_error\n'
    )
    write(root, "ddiver/clients.py", CLIENTS)
    write(root, "ddiver/config.py", settings_source())
    write(
        root,
        "ddiver/runtime.py",
        "from ddiver.clients import connect_with_timeout, make_bot_client, make_user_client\n"
        "from ddiver.config import Settings\n",
    )
    write(
        root,
        "tests/test_cli.py",
        'from ddiver.config import Settings\nPATCH = "ddiver.clients.make_bot_client"\n',
    )


class TestClientsStep:
    def test_core_reads_the_base_settings_and_the_app_keeps_its_own(self, tmp_path):
        repo_after_bot(tmp_path)
        ec.apply_step("clients", root=tmp_path, run=FakeRunner(tmp_path))

        botclient = (tmp_path / CORE_SRC / "botclient.py").read_text()
        assert "from ddiver_core.config import Settings\n" in botclient
        assert "def make_bot_client(settings: Settings)" in botclient
        core_config = (tmp_path / CORE_SRC / "config.py").read_text()
        assert "class Settings(BaseSettings):\n" in core_config
        assert "def load(cls) -> Self:" in core_config and "catchup_hours" not in core_config
        app_config = (tmp_path / "ddiver/config.py").read_text()
        assert "from ddiver_core.config import Settings as CoreSettings\n" in app_config
        assert "class Settings(CoreSettings):\n    catchup_hours: int = 0\n" in app_config
        runtime = (tmp_path / "ddiver/runtime.py").read_text()
        assert runtime == (  # ruff sorts it afterwards; the fake runner does not
            "from ddiver_core.botclient import connect_with_timeout\n"
            "from ddiver.clients import make_bot_client, make_user_client\n"
            "from ddiver.config import Settings\n"
        )
        clients = (tmp_path / "ddiver/clients.py").read_text()
        assert "from ddiver_core.botclient import make_bot_client as make_bot_client" in clients
        assert "def make_user_client(settings: Settings)" in clients
        assert (tmp_path / "tests/test_cli.py").read_text() == (
            'from ddiver.config import Settings\nPATCH = "ddiver.clients.make_bot_client"\n'
        )
        app = (tmp_path / CORE_SRC / "app.py").read_text()
        assert "from ddiver_core.tgerrors import is_fatal_auth_error" in app


RUNTIME = '''"""Runtime."""

import logging

log = logging.getLogger(__name__)


class BotAdapter:
    def mode(self) -> str | None:
        return _parse_mode(self)


def self_id_of(client: object) -> int | None:
    return None


def _parse_mode(action: object) -> str | None:
    return None


def notification_buttons_factory() -> object:
    log.info("buttons")
    return None


class Application:
    def __init__(self) -> None:
        self.parts = (BotAdapter(), self_id_of(None), notification_buttons_factory())
'''

MAIN = '''"""CLI."""

import logging


class JsonFormatter(logging.Formatter):
    pass


def harden_data_dir(path: object) -> None:
    return None


class InstanceLock:
    pass


def describe_config_error(exc: Exception) -> str:
    return str(exc)


def main() -> int:
    harden_data_dir(None)
    return len(describe_config_error(Exception()))
'''

FAKES = '''"""Fakes."""

import datetime as dt

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start


class SentMessage:
    pass


class FakeBotClient:
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []


def make_event(date: dt.datetime = T0) -> object:
    return date
'''


class TestRuntimeStep:
    def test_the_fakes_become_a_dev_only_member_and_the_rest_moves_to_core(self, tmp_path):
        repo_after_scaffold(tmp_path)
        for done in ("timeutil", "links", "matcher", "app"):
            write(tmp_path, f"{CORE_SRC}/{done}.py")
        write(tmp_path, "ddiver/runtime.py", RUNTIME)
        write(tmp_path, "ddiver/__main__.py", MAIN)
        write(tmp_path, "tests/fakes.py", FAKES)
        write(tmp_path, "tests/test_x.py", "from tests.fakes import FakeClock, make_event\n")
        run = FakeRunner(tmp_path)
        report = ec.apply_step("runtime", root=tmp_path, run=run)

        fakes = (tmp_path / TESTING_SRC / "__init__.py").read_text()
        assert fakes.startswith('"""Offline fakes for the tests of apps built on ddiver-core.')
        assert "class FakeClock:" in fakes and "def make_event" not in fakes
        assert "from ddiver_core_testing import T0\n" in (tmp_path / "tests/fakes.py").read_text()
        assert (tmp_path / "tests/test_x.py").read_text() == (
            "from ddiver_core_testing import FakeClock\nfrom tests.fakes import make_event\n"
        )
        runtime = (tmp_path / "ddiver/runtime.py").read_text()
        back = "BotAdapter, self_id_of, notification_buttons_factory"
        assert f"from ddiver_core.botruntime import {back}\n" in runtime
        assert "log = logging.getLogger" in (tmp_path / CORE_SRC / "botruntime.py").read_text()
        main = (tmp_path / "ddiver/__main__.py").read_text()
        assert "from ddiver_core.process import harden_data_dir, describe_config_error" in main
        ci = (tmp_path / ".github/workflows/ci.yml").read_text()
        assert 'dev_only = ("pytest", "ddiver_core_testing")\n' in ci
        uv_calls = [call[1:] for call in run.calls if call[1:] in (["lock"], ["sync"])]
        assert uv_calls == [["lock"], ["sync"]]
        assert f"{TESTING_SRC}/__init__.py" in report.touched
        assert set(run.added()) == set(report.touched)
