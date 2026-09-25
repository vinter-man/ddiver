"""Extract ddiver-core from ddiver in reviewable steps, and be able to do it again.

ddiver keeps moving while the extraction waits for review, so the extraction is a program
rather than a pile of hand edits. Every step derives what it changes from the code it finds:
which files move, what a split carries and what that code needs, which imports and patch
strings point at a moved name. Replaying the extraction onto a newer ddiver head is running
this again, one step per commit. Whatever it cannot do mechanically it refuses, naming the
file and the reason, instead of guessing.

The rules are the ones the ddiver-fb design sets for the extraction:

* Pure moves, no re-export shims. A shim turns ``monkeypatch.setattr("ddiver.x.NAME", ...)``
  into a patch of the shim, which the moved code never reads, so a test keeps passing while
  patching nothing. Every import and every patch string is rewritten to the new home instead.
  The one exception is ``make_bot_client``, documented where it is kept (see :class:`Split`).
* A module that is split keeps importing, by name, whatever it still uses, so the tests'
  ``runtime_module.X`` patches keep reaching the code that reads ``X``. A *kept* name (see
  :class:`Split`) is imported back even though the module itself no longer uses it, because
  another module reads it from there and a test patches it there.
* A class that is only partly shared becomes a base class in ddiver-core, and ddiver keeps a
  subclass under the same name with what only ddiver has (see :class:`BaseSplit`). ddiver and
  its tests go on importing the class they always imported; only code that moved into the core
  is pointed at the base.
* The shared test fakes go to ddiver-core-testing, a workspace member that ddiver depends on
  only in its dev group, so they never reach the image.
* ddiver-core never imports ddiver, the tests or ddiver-core-testing, and ddiver-core-testing
  never imports ddiver or the tests. Checked after every step, together with "a second rewrite
  changes nothing".

Usage, from the repository root, one step per commit::

    uv run python tools/extract_core.py status
    uv run python tools/extract_core.py apply scaffold
    uv run python tools/extract_core.py check
    git commit ...
    uv run python tools/extract_core.py apply leaves
    ...

``apply`` stages exactly the paths it touched (``git mv`` stages the moves) and prints the
step's commit subject. Committing is left to whoever replays it.

This script stays in the repository while branches cut before the extraction are rebased onto
it, because it is how they are rebased. It is deleted in a follow-up commit once they all are.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import fnmatch
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

APP = "ddiver"
CORE = "ddiver_core"
CORE_ROOT = "packages/ddiver-core"
CORE_SRC = f"{CORE_ROOT}/src/{CORE}"
TESTING = "ddiver_core_testing"
TESTING_ROOT = "packages/ddiver-core-testing"
TESTING_SRC = f"{TESTING_ROOT}/src/{TESTING}"
SOURCES = {CORE: CORE_SRC, TESTING: TESTING_SRC}
# Imports and patch strings are rewritten under these. The tool and its own tests are not:
# they spell the old paths on purpose.
REWRITE_ROOTS = (APP, "tests", "scripts", f"{CORE_ROOT}/src", f"{TESTING_ROOT}/src")
NEVER_REWRITE = frozenset({"tests/test_extract_core.py"})
# What each package must never import: the core is installed without the app, the tests or the
# fakes, and the fakes are installed without the app and the tests.
FORBIDDEN = {CORE_SRC: frozenset({APP, "tests", TESTING}), TESTING_SRC: frozenset({APP, "tests"})}

DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
SPHINX_ROLE = re.compile(
    r"(:(?:mod|class|func|meth|attr|data|exc|obj):`~?)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(`)"
)


class ExtractError(Exception):
    """A change the script will not make mechanically; the message says what and where."""


# -- running other tools ----------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str = ""


class Runner(Protocol):
    """How the script runs git, ruff and uv; the tests pass a recorder instead."""

    def __call__(self, args: Sequence[str], *, cwd: Path) -> Result: ...


def run_subprocess(args: Sequence[str], *, cwd: Path) -> Result:
    done = subprocess.run(  # noqa: S603 - fixed tool invocations built by this script
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    return Result(done.returncode, done.stdout, done.stderr)


def must(run: Runner, args: Sequence[str], *, cwd: Path) -> str:
    result = run(args, cwd=cwd)
    if result.code != 0:
        raise ExtractError(f"`{' '.join(args)}` exited {result.code}:\n{result.out}{result.err}")
    return result.out


def ruff(*args: str) -> list[str]:
    return [sys.executable, "-m", "ruff", *args]


def uv(*args: str) -> list[str]:
    # `uv run` exports the path of the uv that started us; fall back to PATH otherwise.
    return [os.environ.get("UV", "uv"), *args]


# -- what moves -------------------------------------------------------------------------------


def module_of(path: str) -> str:
    """The dotted name of a repository file: ``ddiver/bot/__init__.py`` is ``ddiver.bot``."""
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass(frozen=True)
class Split:
    """Top-level definitions carried out of a module that otherwise stays where it is.

    ``kept`` names are imported back into the source module even when it no longer uses them,
    because other code reads them from there and a test patches them there. ``target`` is a
    module of ``package``; an empty one is the package itself, its ``__init__``.
    """

    source: str
    target: str
    names: tuple[str, ...]
    docstring: str
    kept: tuple[str, ...] = ()
    kept_reason: str = ""
    source_docstring: str | None = None
    ignores_reason: str = ""
    package: str = CORE

    @property
    def source_module(self) -> str:
        return module_of(self.source)

    @property
    def target_module(self) -> str:
        return f"{self.package}.{self.target}" if self.target else self.package

    @property
    def target_path(self) -> str:
        return f"{SOURCES[self.package]}/{self.target or '__init__'}.py"


@dataclass(frozen=True)
class BaseSplit:
    """A class whose shared members become a base class of the same name in ddiver-core.

    The source keeps its class, now a subclass of the core's (imported as ``alias``), with the
    ``stays`` members. Every member has to be named in ``names`` or ``stays``: a member added
    upstream is refused until somebody decides which side it belongs to, rather than guessed.
    A carried method annotated to return the class returns ``Self`` instead, so that the
    subclass's ``load()`` is typed as the subclass.
    """

    source: str
    target: str
    cls: str
    names: tuple[str, ...]
    stays: tuple[str, ...]
    alias: str
    docstring: str
    source_docstring: str
    ignores_reason: str = ""

    @property
    def source_module(self) -> str:
        return module_of(self.source)

    @property
    def target_module(self) -> str:
        return f"{CORE}.{self.target}"

    @property
    def target_path(self) -> str:
        return f"{CORE_SRC}/{self.target}.py"


@dataclass(frozen=True)
class Member:
    """A workspace package a step creates: its files, and the edits that let the build see it."""

    root: str
    files: tuple[tuple[str, str], ...]
    edits: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]


@dataclass(frozen=True)
class Step:
    key: str
    subject: str
    moves: tuple[str, ...] = ()  # modules or packages directly under ddiver/, moved whole
    splits: tuple[Split, ...] = ()
    bases: tuple[BaseSplit, ...] = ()
    members: tuple[Member, ...] = ()


TGERRORS = Split(
    source=f"{APP}/clients.py",
    target="tgerrors",
    names=("is_fatal_auth_error", "is_session_file_error"),
    docstring="""Telethon errors after which restarting cannot help.

The supervisor (``ddiver_core.app``) parks itself in degraded mode instead of restarting when
one of these ends a run: a dead session or a locked session file only gets worse with retries.
They are Telethon's error classes and nothing else, so they sit apart from the code that builds
the listener's client.""",
)

BOTCLIENT = Split(
    source=f"{APP}/clients.py",
    target="botclient",
    names=("CONNECT_TIMEOUT", "BOT_FLOOD_THRESHOLD", "connect_with_timeout", "make_bot_client"),
    kept=("make_bot_client",),
    kept_reason=(
        "Kept as a name in this module rather than imported from ddiver-core where it is used:"
        " the CLI and the runtime read make_bot_client from here, and test_cli patches it here."
    ),
    docstring="""The bot's Telethon client, and a connect that cannot hang.

Every app built on ddiver-core talks to its users through a BotFather bot driven over MTProto,
so the bot client is shared; a listener's user client is not, and stays with the app that has
one. ``flood_sleep_threshold=0`` matters: delivery owns the retry policy and has to see every
flood wait instead of having Telethon sleep through it.""",
    source_docstring="""The listener's Telethon user client: construction and reconnect tracking.

The bot client, ``connect_with_timeout`` and the fatal-error classifiers live in ddiver-core
(``ddiver_core.botclient``, ``ddiver_core.tgerrors``).""",
)

SETTINGS = BaseSplit(
    source=f"{APP}/config.py",
    target="config",
    cls="Settings",
    names=(
        "model_config",
        "api_id",
        "api_hash",
        "bot_token",
        "owner_id",
        "data_dir",
        "log_level",
        "log_format",
        "metrics_host",
        "metrics_port",
        "ping_url",
        "ping_timeout",
        "ping_interval",
        "digest_threshold",
        "regex_timeout_ms",
        "_bot_token_shape",
        "_log_format",
        "_ping_url",
        "_log_level",
        "load",
        "bot_session",
        "db_path",
        "heartbeat_path",
        "regex_timeout",
        "bot_id",
    ),
    stays=("catchup_hours", "catchup_max_per_chat", "ignore_own_messages", "user_session"),
    alias="CoreSettings",
    docstring="""Settings every app on ddiver-core has, from environment variables / ``.env``.

The bot (its BotFather token, and the ``api_id``/``api_hash`` pair its MTProto login needs), the
owner, the data directory with the bot session, the database and the heartbeat in it, logging,
metrics, the dead-man's-switch ping, and the delivery and matching limits. An app subclasses
:class:`Settings` with what only it has. A field's environment variable is its name, so which of
the two classes declares a field changes no variable anybody has set.""",
    source_docstring="""ddiver's settings: the core's, plus the user session and catch-up.

Everything the bot, delivery and matching need is ``ddiver_core.config.Settings``; this subclass
adds what only an app with a Telegram user session has. Environment variable names are the field
names, as they were before the split.""",
)

BOTRUNTIME = Split(
    source=f"{APP}/runtime.py",
    target="botruntime",
    names=("BotAdapter", "self_id_of", "_parse_mode", "notification_buttons_factory"),
    docstring="""The bot half of the Telegram adapter: router actions and buttons as Telethon calls.

Handlers in ``ddiver_core.bot`` return plain actions; :class:`BotAdapter` performs them with a
real bot client, and ``notification_buttons_factory`` turns an outbox row into the keyboard
under a notification. None of it touches a listener's session, which is why it is shared; the
composition root that owns the clients and the background tasks stays with each app.""",
)

PROCESS = Split(
    source=f"{APP}/__main__.py",
    target="process",
    names=("JsonFormatter", "harden_data_dir", "InstanceLock", "describe_config_error"),
    ignores_reason=(
        "InstanceLock imports fcntl only when it takes the lock, and a configuration error must"
        " be reported even when the error object itself is broken."
    ),
    docstring="""Process hygiene every ddiver-core app needs before it opens anything.

One process per data directory (two would poison a Telethon session file), a data directory
only its owner can read (the session file is the account), log lines a shipper can index, and
configuration errors that name the setting without echoing its value.""",
)

FAKES = Split(
    source="tests/fakes.py",
    target="",
    package=TESTING,
    names=("T0", "FakeClock", "SentMessage", "FakeBotClient"),
    docstring="""Offline fakes for the tests of apps built on ddiver-core.

A clock that moves only when a test says so, and a bot client that records what it was asked
to send and can be told to fail or to stall. This package is a dev dependency and is never
installed in an image. The Telethon *event* fakes stay with the app that listens to Telegram:
they describe its source, not the shared core.""",
    source_docstring="""Offline Telethon events and a fake user client, built without a connection.

The clock and the bot client fakes are shared with every app on ddiver-core and live in
``ddiver_core_testing``, a dev-only package.""",
)

TESTING_PYPROJECT = f"""\
[project]
name = "ddiver-core-testing"
version = "0.1.0"
description = "Offline fakes for tests of apps on ddiver-core. Dev only; never in an image."
license = "MIT"
requires-python = ">=3.12"
authors = [{{ name = "vinter-man" }}]
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{TESTING}"]
"""

TESTING_MEMBER = Member(
    root=TESTING_ROOT,
    files=(("pyproject.toml", TESTING_PYPROJECT), (f"src/{TESTING}/py.typed", "")),
    edits=(
        (
            "pyproject.toml",
            (
                (
                    f'members = ["{CORE_ROOT}"]',
                    f'members = ["{CORE_ROOT}", "{TESTING_ROOT}"]',
                ),
                (
                    "ddiver-core = { workspace = true }\n",
                    (
                        "ddiver-core = { workspace = true }\n"
                        "ddiver-core-testing = { workspace = true }\n"
                    ),
                ),
                (
                    "dev = [\n",
                    'dev = [\n  "ddiver-core-testing",  # the shared fakes, never in the image\n',
                ),
                (
                    f'src = ["ddiver", "{CORE_ROOT}/src", "tests"]',
                    f'src = ["ddiver", "{CORE_ROOT}/src", "{TESTING_ROOT}/src", "tests"]',
                ),
                (
                    f'known-first-party = ["ddiver", "{CORE}"]',
                    f'known-first-party = ["ddiver", "{CORE}", "{TESTING}"]',
                ),
                (
                    f'files = ["ddiver", "{CORE_ROOT}/src", "tests", "tools"]',
                    (
                        f'files = ["ddiver", "{CORE_ROOT}/src", "{TESTING_ROOT}/src", "tests",'
                        ' "tools"]'
                    ),
                ),
            ),
        ),
        (
            "Dockerfile",
            (
                (
                    f"COPY {CORE_ROOT}/pyproject.toml ./{CORE_ROOT}/\n",
                    (
                        f"COPY {CORE_ROOT}/pyproject.toml ./{CORE_ROOT}/\n"
                        f"COPY {TESTING_ROOT}/pyproject.toml ./{TESTING_ROOT}/\n"
                    ),
                ),
            ),
        ),
        (
            ".dockerignore",
            (
                (
                    "tools/\n",
                    (
                        "tools/\n"
                        "# The shared test fakes are a dev dependency: uv reads their pyproject\n"
                        "# with the rest of the workspace, and their code stays out of the image.\n"
                        f"{TESTING_ROOT}/src/\n"
                    ),
                ),
            ),
        ),
        (
            ".github/workflows/ci.yml",
            (('dev_only = ("pytest",)\n', f'dev_only = ("pytest", "{TESTING}")\n'),),
        ),
    ),
)

STEPS: tuple[Step, ...] = (
    Step("scaffold", "Make ddiver a uv workspace with an empty ddiver-core package"),
    Step(
        "leaves",
        "Move the leaf modules into ddiver-core",
        moves=("timeutil", "rules", "richtext", "i18n", "watchdog", "health", "metrics"),
    ),
    Step("storage", "Move links and storage into ddiver-core", moves=("links", "storage")),
    Step(
        "bot",
        "Move matching, the bot UI and delivery into ddiver-core",
        moves=("matcher", "portable", "bot", "formatter", "senders", "delivery"),
    ),
    Step(
        "clients",
        "Move the supervisor, shared settings, bot client and error classifiers into ddiver-core",
        moves=("app",),
        splits=(TGERRORS, BOTCLIENT),
        bases=(SETTINGS,),
    ),
    Step(
        "runtime",
        "Move the bot adapter and process hygiene into ddiver-core, the fakes into a dev package",
        splits=(BOTRUNTIME, PROCESS, FAKES),
        members=(TESTING_MEMBER,),
    ),
)


def step_named(key: str) -> tuple[int, Step]:
    for index, step in enumerate(STEPS):
        if step.key == key:
            return index, step
    raise ExtractError(f"no step {key!r}; the steps are {', '.join(s.key for s in STEPS)}")


# -- where things went ------------------------------------------------------------------------


@dataclass(frozen=True)
class Relocations:
    """Every move and split applied so far, as the rewriter needs to see them."""

    modules: dict[str, str] = field(default_factory=dict)
    symbols: dict[tuple[str, str], str] = field(default_factory=dict)
    kept: frozenset[tuple[str, str]] = frozenset()
    # Split names the old module still imports by name, so a patch there still works.
    bound: frozenset[tuple[str, str]] = frozenset()
    # Classes that stayed in the app as subclasses of a core base of the same name. Only code
    # inside the packages is pointed at the base; the app keeps reading its own class.
    bases: dict[tuple[str, str], str] = field(default_factory=dict)

    def in_packages(self) -> Relocations:
        """The view for a file of ddiver-core or ddiver-core-testing: bases are where it reads."""
        if not self.bases:
            return self
        return dataclasses.replace(self, symbols={**self.symbols, **self.bases}, bases={})

    def module(self, dotted: str) -> str | None:
        """The new name of a module that moved, itself or inside a package; else None."""
        for old in sorted(self.modules, key=len, reverse=True):
            if dotted == old or dotted.startswith(f"{old}."):
                return self.modules[old] + dotted[len(old) :]
        return None

    def import_source(self, module: str, name: str) -> str:
        """Where ``from <module> import <name>`` has to read from now."""
        if (module, name) in self.kept:
            return module
        if (module, name) in self.symbols:
            return self.symbols[module, name]
        moved = self.module(module)
        if moved is not None:
            return moved
        submodule = self.module(f"{module}.{name}")
        if submodule is not None:
            return submodule.rpartition(".")[0]
        return module

    def patch_target(self, dotted: str) -> str | None:
        """Where a ``"package.module.NAME"`` patch string has to point now, or None.

        A bare module path is never rewritten: ``"ddiver.config"`` is also the export format
        tag, and a module on its own is not something a test patches.
        """
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            prefix, rest = ".".join(parts[:cut]), parts[cut:]
            moved = self.module(prefix)
            if moved is not None:
                return ".".join([moved, *rest])
            pair = (prefix, rest[0])
            if pair in self.symbols and pair not in self.kept and pair not in self.bound:
                return ".".join([self.symbols[pair], *rest])
        return None

    def reference(self, dotted: str) -> str | None:
        """Where a documentation cross-reference has to point: the definition's new home."""
        moved = self.module(dotted)
        if moved is not None:
            return moved
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            pair = (".".join(parts[:cut]), parts[cut])
            if pair in self.symbols:
                return ".".join([self.symbols[pair], *parts[cut:]])
        return None


def relocations_through(index: int, root: Path) -> Relocations:
    modules: dict[str, str] = {}
    symbols: dict[tuple[str, str], str] = {}
    kept: set[tuple[str, str]] = set()
    bound: set[tuple[str, str]] = set()
    bases: dict[tuple[str, str], str] = {}
    for step in STEPS[: index + 1]:
        modules.update({f"{APP}.{name}": f"{CORE}.{name}" for name in step.moves})
        for split in step.splits:
            symbols.update({(split.source_module, n): split.target_module for n in split.names})
            kept.update((split.source_module, n) for n in split.kept)
            bound.update(
                (split.source_module, n)
                for n in imported_from(root / split.source, split.target_module)
            )
        bases.update({(b.source_module, b.cls): b.target_module for b in step.bases})
    return Relocations(modules, symbols, frozenset(kept), frozenset(bound), bases)


def view_for(rel: str, reloc: Relocations) -> Relocations:
    return reloc.in_packages() if rel.startswith("packages/") else reloc


def imported_from(path: Path, module: str) -> set[str]:
    """Names a file binds at top level with ``from <module> import ...``."""
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == module and not node.level
        for alias in node.names
    }


# -- rewriting imports, patch strings and cross-references ------------------------------------


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    text: bytes


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    starts.extend(i + 1 for i, byte in enumerate(data) if byte == ord("\n"))
    return starts


def _span(node: ast.expr | ast.stmt, starts: list[int]) -> tuple[int, int]:
    # ast offsets are UTF-8 byte columns, and these files are full of Cyrillic: work on bytes.
    end_line = node.end_lineno if node.end_lineno is not None else node.lineno
    end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
    return starts[node.lineno - 1] + node.col_offset, starts[end_line - 1] + end_col


def _alias(alias: ast.alias) -> str:
    return alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"


def _rewrite_import_from(
    node: ast.ImportFrom, data: bytes, starts: list[int], reloc: Relocations, path: str
) -> _Edit | None:
    if node.level:
        raise ExtractError(f"{path}:{node.lineno}: relative import; only absolute ones are handled")
    module = node.module or ""
    groups: dict[str, list[ast.alias]] = {}
    for alias in node.names:
        groups.setdefault(reloc.import_source(module, alias.name), []).append(alias)
    if list(groups) == [module]:
        return None
    start, end = _span(node, starts)
    segment = data[start:end]
    if len(groups) == 1:
        # Only the module name changes: keep the statement's own layout and comments.
        (new,) = groups
        match = re.match(rb"from(\s+)" + re.escape(module.encode()) + rb"(?=\s)", segment)
        if match is None:
            raise ExtractError(f"{path}:{node.lineno}: cannot find the module name to replace")
        return _Edit(start, start + match.end(), b"from" + match[1] + new.encode())
    if b"#" in segment:
        raise ExtractError(
            f"{path}:{node.lineno}: an import that has to be split carries a comment;"
            " split it by hand so the comment lands where it belongs"
        )
    separator = "\n" + " " * node.col_offset
    text = separator.join(
        f"from {new} import {', '.join(_alias(a) for a in aliases)}"
        for new, aliases in groups.items()
    )
    return _Edit(start, end, text.encode())


def _rewrite_import(
    node: ast.Import, starts: list[int], reloc: Relocations, path: str
) -> _Edit | None:
    parts: list[str] = []
    changed = False
    for alias in node.names:
        new = reloc.module(alias.name)
        if new is None:
            parts.append(_alias(alias))
            continue
        if alias.asname is None:
            raise ExtractError(
                f"{path}:{node.lineno}: `import {alias.name}` without an alias; the code uses"
                " the dotted name, which a rewrite of the import alone would not fix"
            )
        parts.append(f"{new} as {alias.asname}")
        changed = True
    if not changed:
        return None
    # An import statement cannot be parenthesised, so a comment can only follow it, outside
    # the span replaced here.
    start, end = _span(node, starts)
    return _Edit(start, end, f"import {', '.join(parts)}".encode())


def _rewrite_patch_string(
    node: ast.Constant, data: bytes, starts: list[int], reloc: Relocations, path: str
) -> _Edit | None:
    value = node.value
    if not isinstance(value, str) or not DOTTED.fullmatch(value):
        return None
    new = reloc.patch_target(value)
    if new is None:
        return None
    start, end = _span(node, starts)
    segment = data[start:end].decode()
    if segment.count(value) != 1:
        raise ExtractError(f"{path}:{node.lineno}: {value!r} is not spelled out in its literal")
    return _Edit(start, end, segment.replace(value, new).encode())


def _apply_edits(data: bytes, edits: list[_Edit]) -> bytes:
    out: list[bytes] = []
    position = 0
    for edit in sorted(edits, key=lambda e: e.start):
        if edit.start < position:
            raise ExtractError("overlapping rewrites; this is a bug in the script")
        out.extend((data[position : edit.start], edit.text))
        position = edit.end
    out.append(data[position:])
    return b"".join(out)


def rewrite_source(
    source: str, reloc: Relocations, *, path: str = "<source>"
) -> tuple[str, list[str]]:
    """Point every import, patch string and cross-reference at where the name lives now.

    Returns the new text and one line per rewritten string or reference, for the operator to
    read: those are the rewrites a person should look at, imports are not.
    """
    data = source.encode()
    starts = _line_starts(data)
    edits: list[_Edit] = []
    notes: list[str] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        edit: _Edit | None = None
        if isinstance(node, ast.ImportFrom):
            edit = _rewrite_import_from(node, data, starts, reloc, path)
        elif isinstance(node, ast.Import):
            edit = _rewrite_import(node, starts, reloc, path)
        elif isinstance(node, ast.Constant):
            edit = _rewrite_patch_string(node, data, starts, reloc, path)
            if edit is not None:
                notes.append(f"{path}:{node.lineno}: {edit.text.decode()}")
        if edit is not None:
            edits.append(edit)
    text = _apply_edits(data, edits).decode()

    def role(match: re.Match[str]) -> str:
        new = reloc.reference(match[2])
        if new is None:
            return match[0]
        notes.append(f"{path}: {match[0]} -> {new}")
        return f"{match[1]}{new}{match[3]}"

    return SPHINX_ROLE.sub(role, text), notes


def python_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for base in REWRITE_ROOTS:
        found.update(p for p in (root / base).rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(found)


def rewrite_tree(root: Path, reloc: Relocations) -> tuple[set[str], list[str]]:
    touched: set[str] = set()
    notes: list[str] = []
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in NEVER_REWRITE:
            continue
        text = path.read_text(encoding="utf-8")
        new, file_notes = rewrite_source(text, view_for(rel, reloc), path=rel)
        if new != text:
            path.write_text(new, encoding="utf-8")
            touched.add(rel)
            notes.extend(file_notes)
    return touched, notes


# -- splitting a module -----------------------------------------------------------------------


@dataclass(frozen=True)
class SplitResult:
    module_text: str
    source_text: str
    imported_back: tuple[str, ...]


def _end(node: ast.stmt) -> int:
    return node.end_lineno if node.end_lineno is not None else node.lineno


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found[node.name] = node
        elif isinstance(node, ast.Assign):
            found.update(
                (leaf.id, node)
                for target in node.targets
                for leaf in ast.walk(target)
                if isinstance(leaf, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
        elif isinstance(node, ast.TypeAlias):
            found[node.name.id] = node
    return found


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _block_lines(node: ast.stmt, lines: list[str]) -> tuple[int, int]:
    """0-based [first, end) lines of a definition, with its decorators and the comment above."""
    decorators = getattr(node, "decorator_list", [])
    first = min([node.lineno, *(d.lineno for d in decorators)]) - 1
    while first > 0 and lines[first - 1].lstrip().startswith("#"):
        first -= 1
    return first, _end(node)


def _docstring_literal(text: str) -> str:
    return f'"""{text}\n"""' if "\n" in text else f'"""{text}"""'


def _comment(text: str) -> str:
    return "".join(f"# {line}\n" for line in textwrap.wrap(text, width=94))


def _refuse_conditional_imports(tree: ast.Module, path: str) -> None:
    for node in tree.body:
        if isinstance(node, ast.If | ast.Try | ast.With) and any(
            isinstance(n, ast.Import | ast.ImportFrom) for n in ast.walk(node)
        ):
            raise ExtractError(f"{path}:{node.lineno}: conditional imports; split this by hand")


def _bound(alias: ast.alias) -> str:
    """The name an import alias binds: ``import a.b`` binds ``a``, ``import a as b`` binds ``b``."""
    return alias.asname if alias.asname is not None else alias.name.split(".")[0]


def _imports_text(tree: ast.Module, lines: list[str], used: set[str]) -> str:
    """The source's top-level imports that bind a name in ``used``, and ``__future__`` ones.

    Only those: ruff does not drop an unused import from an ``__init__.py``, where it may be a
    re-export, so a module split into a package's ``__init__`` must not start out with them.
    """
    out: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            wanted = list(node.names)
        else:
            wanted = [alias for alias in node.names if _bound(alias) in used]
        if len(wanted) == len(node.names):
            out.append("".join(lines[node.lineno - 1 : _end(node)]))
        elif isinstance(node, ast.ImportFrom) and wanted:
            names = ", ".join(_alias(alias) for alias in wanted)
            out.append(f"from {'.' * node.level}{node.module or ''} import {names}\n")
        elif wanted:
            out.append(f"import {', '.join(_alias(alias) for alias in wanted)}\n")
    return "".join(out).rstrip("\n")


def _after_last_import(tree: ast.Module) -> int:
    ends = [_end(n) for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    return max(ends, default=0)


def split_source(source: str, split: Split, *, path: str) -> SplitResult:
    """Cut ``split.names`` out of ``source`` into a new module, and patch up what is left.

    The new module gets the source's top-level imports that the moved code uses, and the
    source's module logger if the moved code logs. The source imports back, by name, every
    moved name it still uses, plus the kept ones; ruff drops what it no longer needs.
    """
    tree = ast.parse(source, filename=path)
    lines = source.splitlines(keepends=True)
    defined = _top_level_definitions(tree)
    moved = _moved_nodes(tree, defined, split, path)
    blocks = sorted(((_block_lines(node, lines), node) for node in moved), key=lambda b: b[0])
    module_text = _new_module(split, tree, lines, defined, blocks)
    source_text, back = _trimmed_source(split, lines, [block for block, _ in blocks], path)
    return SplitResult(module_text, source_text, back)


def _moved_nodes(
    tree: ast.Module, defined: dict[str, ast.stmt], split: Split, path: str
) -> list[ast.stmt]:
    missing = [name for name in split.names if name not in defined]
    if missing:
        raise ExtractError(f"{path}: {', '.join(missing)} not defined at top level any more")
    _refuse_conditional_imports(tree, path)
    moved = list({id(defined[name]): defined[name] for name in split.names}.values())
    needs = set().union(*(_names_used(node) for node in moved))
    behind = sorted((needs & defined.keys()) - set(split.names) - {"log"})
    if behind:
        raise ExtractError(
            f"{path}: the moved code uses {', '.join(behind)}, which would stay behind;"
            " add it to the split"
        )
    return moved


def _new_module(
    split: Split,
    tree: ast.Module,
    lines: list[str],
    defined: dict[str, ast.stmt],
    blocks: list[tuple[tuple[int, int], ast.stmt]],
) -> str:
    items: list[tuple[str, str]] = []
    needs = set().union(*(_names_used(node) for _, node in blocks))
    if "log" in needs and "log" in defined:
        logger = defined["log"]
        needs |= _names_used(logger)
        items.append(("log", "".join(lines[logger.lineno - 1 : _end(logger)])))
    text = _docstring_literal(split.docstring) + "\n\n" + _imports_text(tree, lines, needs)
    for (first, end), node in blocks:
        definition = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        items.append(("definition" if definition else "statement", "".join(lines[first:end])))
    previous = "imports"
    for kind, block in items:
        # The layout the source had: constants together, one line after the logger and the
        # imports, two around definitions.
        if kind == "definition" or previous == "definition":
            gap = "\n\n\n"
        elif kind == "statement" and previous == "statement":
            gap = "\n"
        else:
            gap = "\n\n"
        text += gap + block.strip("\n")
        previous = kind
    return text + "\n"


def _trimmed_source(
    split: Split, lines: list[str], ranges: list[tuple[int, int]], path: str
) -> tuple[str, tuple[str, ...]]:
    dropped: set[int] = set()
    for first, end in ranges:
        dropped.update(range(first, end))
        after = end
        while after < len(lines) and not lines[after].strip():
            dropped.add(after)
            after += 1
    remaining = [line for number, line in enumerate(lines) if number not in dropped]
    rest = ast.parse("".join(remaining), filename=path)
    used = _names_used(rest)
    back = tuple(name for name in split.names if name in used or name in split.kept)
    statements: list[str] = []
    if plain := [name for name in back if name not in split.kept]:
        statements.append(f"from {split.target_module} import {', '.join(plain)}\n")
    if kept := [name for name in back if name in split.kept]:
        # `name as name` is the explicit re-export mypy's no_implicit_reexport asks for, and
        # exactly what ruff's useless-alias rule exists to flag everywhere else.
        aliases = ", ".join(f"{name} as {name}" for name in kept)
        statements.append(
            _comment(split.kept_reason)
            + f"from {split.target_module} import {aliases}  # noqa: PLC0414\n"
        )
    at = _after_last_import(rest)
    remaining[at:at] = statements
    if split.source_docstring is not None:
        _replace_docstring(remaining, rest, split.source_docstring, path)
    return "".join(remaining), back


def _replace_docstring(lines: list[str], tree: ast.Module, text: str, path: str) -> None:
    first = tree.body[0] if tree.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        raise ExtractError(f"{path}: no module docstring to replace")
    lines[first.lineno - 1 : _end(first)] = [_docstring_literal(text) + "\n"]


def apply_split(split: Split, root: Path) -> set[str]:
    source = root / split.source
    target = root / split.target_path
    if target.exists():
        raise ExtractError(f"{split.target_path} already exists")
    result = split_source(source.read_text(encoding="utf-8"), split, path=split.source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.module_text, encoding="utf-8")
    source.write_text(result.source_text, encoding="utf-8")
    return {split.source, split.target_path}


# -- splitting a class into a core base and an app subclass -----------------------------------


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _member_name(node: ast.stmt, path: str) -> str:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    raise ExtractError(f"{path}:{node.lineno}: a class member without one name; split it by hand")


def _class_named(tree: ast.Module, name: str, path: str) -> ast.ClassDef:
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    if len(found) != 1:
        raise ExtractError(f"{path}: expected one top-level class {name}, found {len(found)}")
    node = found[0]
    if node.decorator_list:
        raise ExtractError(f"{path}:{node.lineno}: {name} is decorated; split it by hand")
    header_end = max(
        [node.lineno, *(_end_of(b) for b in node.bases), *(_end_of(k) for k in node.keywords)]
    )
    if header_end != node.lineno:
        raise ExtractError(f"{path}:{node.lineno}: {name}'s header spans lines; split it by hand")
    return node


def _end_of(node: ast.expr | ast.keyword) -> int:
    return node.end_lineno if node.end_lineno is not None else node.lineno


def _check_carried(
    carried: list[tuple[str, ast.stmt]],
    spec: BaseSplit,
    defined: dict[str, ast.stmt],
    path: str,
) -> None:
    stays = set(spec.stays)
    for name, node in carried:
        reads = {
            n.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in {"self", "cls"}
        }
        if reads & stays:
            raise ExtractError(
                f"{path}: {name} reads {', '.join(sorted(reads & stays))}, which stays in the"
                " subclass; the base would not have it"
            )
        decorated = {
            arg.value
            for decorator in getattr(node, "decorator_list", [])
            if isinstance(decorator, ast.Call)
            for arg in decorator.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        if decorated & stays:
            raise ExtractError(
                f"{path}: {name} is declared for {', '.join(sorted(decorated & stays))}, which"
                " stays in the subclass"
            )
        behind = sorted((_names_used(node) & defined.keys()) - {spec.cls})
        if behind:
            raise ExtractError(
                f"{path}: {name} uses {', '.join(behind)}, which would stay behind in {spec.source}"
            )


def _returns_self(
    carried: list[tuple[str, ast.stmt]], lines: list[str], cls: str, path: str
) -> tuple[list[str], bool]:
    """Carried methods annotated to return the class return ``Self``: the subclass inherits
    them, and ``Settings.load()`` has to be typed as whichever class it was called on."""
    out = list(lines)
    changed = False
    for _, node in carried:
        returns = getattr(node, "returns", None)
        if not isinstance(returns, ast.Name | ast.Constant):
            continue
        if (returns.id if isinstance(returns, ast.Name) else returns.value) != cls:
            continue
        if returns.end_lineno != returns.lineno or returns.end_col_offset is None:
            raise ExtractError(f"{path}:{returns.lineno}: a return annotation over several lines")
        row = out[returns.lineno - 1].encode()
        out[returns.lineno - 1] = (
            row[: returns.col_offset] + b"Self" + row[returns.end_col_offset :]
        ).decode()
        changed = True
    return out, changed


def _imports_self(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"typing", "typing_extensions"}
        and any(alias.name == "Self" and alias.asname is None for alias in node.names)
        for node in tree.body
    )


def _classified(
    node: ast.ClassDef, spec: BaseSplit, path: str
) -> tuple[list[tuple[str, ast.stmt]], ast.stmt | None]:
    """The class's members with their names, and its docstring; refuses an unclassified one."""
    body = list(node.body)
    class_doc = body.pop(0) if body and _is_docstring(body[0]) else None
    members = [(_member_name(stmt, path), stmt) for stmt in body]
    names = [name for name, _ in members]
    unknown = [n for n in names if n not in spec.names and n not in spec.stays]
    if unknown:
        raise ExtractError(
            f"{path}: {spec.cls} has {', '.join(unknown)}, which is neither carried to the base"
            " nor kept in the subclass; decide where it belongs"
        )
    missing = [n for n in (*spec.names, *spec.stays) if n not in names]
    if missing:
        raise ExtractError(f"{path}: {', '.join(missing)} not in {spec.cls} any more")
    if not any(name in spec.stays for name in names):
        raise ExtractError(f"{path}: nothing stays in {spec.cls}; move the module instead")
    return members, class_doc


def _regions(
    members: list[tuple[str, ast.stmt]], lines: list[str], start: int
) -> list[tuple[str, list[str]]]:
    """Each member owns the lines from the end of the one before it: its blank lines, the
    comment above it, its decorators and itself."""
    regions: list[tuple[str, list[str]]] = []
    for name, stmt in members:
        regions.append((name, lines[start : _end(stmt)]))
        start = _end(stmt)
    return regions


def _body(regions: list[tuple[str, list[str]]], wanted: Iterable[str]) -> str:
    """The wanted members in source order. One that followed a member which went to the other
    class opens the group that member opened, if it opened one: fields stay grouped as they
    were instead of running into the group before them."""
    chosen = set(wanted)
    out: list[str] = []
    gap: list[str] = []
    for name, region in regions:
        lead = list(itertools.takewhile(lambda line: not line.strip(), region))
        if name not in chosen:
            gap = gap or lead
            continue
        out.extend(region if lead or not out else [*gap, *region])
        gap = []
    return "".join(itertools.dropwhile(lambda line: not line.strip(), out))


def split_class(source: str, spec: BaseSplit, *, path: str) -> SplitResult:
    """Carry ``spec.names`` of a class into a same-named base class in a new core module.

    The new module gets its own docstring, the source's top-level imports that the carried
    members and the class header use, and the class header as it was, bases and all. The source
    keeps a class of the same name deriving from the new one, with the members that stay, each
    with the blank lines and comments it had above it.
    """
    tree = ast.parse(source, filename=path)
    lines = source.splitlines(keepends=True)
    _refuse_conditional_imports(tree, path)
    node = _class_named(tree, spec.cls, path)
    members, class_doc = _classified(node, spec, path)
    carried = [(n, s) for n, s in members if n in spec.names]
    _check_carried(carried, spec, _top_level_definitions(tree), path)
    lines, needs_self = _returns_self(carried, lines, spec.cls, path)
    regions = _regions(members, lines, _end(class_doc) if class_doc else node.lineno)

    header = [*node.bases, *(keyword.value for keyword in node.keywords)]
    used = set().union(*(_names_used(n) for n in [*header, *(stmt for _, stmt in carried)]))
    if needs_self:
        used.add("Self")
    imports = _imports_text(tree, lines, used)
    if needs_self and not _imports_self(tree):
        imports += "\nfrom typing import Self"
    module_text = (
        f"{_docstring_literal(spec.docstring)}\n\n{imports}\n\n\n"
        + lines[node.lineno - 1]
        + _body(regions, spec.names)
    )

    doc = "".join(lines[node.lineno : _end(class_doc)]) + "\n" if class_doc else ""
    subclass = f"class {spec.cls}({spec.alias}):\n{doc}{_body(regions, spec.stays)}"
    remaining = list(lines)
    remaining[node.lineno - 1 : _end(node)] = [subclass]
    at = _after_last_import(tree)
    remaining[at:at] = [f"from {spec.target_module} import {spec.cls} as {spec.alias}\n"]
    _replace_docstring(remaining, tree, spec.source_docstring, path)
    return SplitResult(module_text, "".join(remaining), (spec.cls,))


def apply_base(spec: BaseSplit, root: Path) -> set[str]:
    source = root / spec.source
    target = root / spec.target_path
    if target.exists():
        raise ExtractError(f"{spec.target_path} already exists")
    result = split_class(source.read_text(encoding="utf-8"), spec, path=spec.source)
    target.write_text(result.module_text, encoding="utf-8")
    source.write_text(result.source_text, encoding="utf-8")
    return {spec.source, spec.target_path}


# -- moving whole modules ---------------------------------------------------------------------


def _remove_leftovers(directory: Path) -> None:
    """A directory left behind would still import, as a namespace package, and hide a missed
    rewrite behind an empty ``ddiver.bot``."""
    for cache in sorted(directory.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)
    leftover = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))
    if any((directory / p).is_file() for p in leftover):
        raise ExtractError(f"untracked files left in {directory}: {', '.join(leftover)}")
    for sub in sorted((directory / p for p in leftover), reverse=True):
        sub.rmdir()
    directory.rmdir()


def move_module(name: str, *, root: Path, run: Runner) -> set[str]:
    if (root / APP / f"{name}.py").is_file():
        pairs = [(f"{APP}/{name}.py", f"{CORE_SRC}/{name}.py")]
    elif (root / APP / name).is_dir():
        tracked = must(run, ["git", "ls-files", "--", f"{APP}/{name}"], cwd=root).split()
        pairs = [(old, f"{CORE_SRC}/{old.removeprefix(APP + '/')}") for old in tracked]
    else:
        raise ExtractError(f"{APP}/{name} is neither a module nor a package here")
    if not pairs:
        raise ExtractError(f"{APP}/{name} has no tracked files")
    for old, new in pairs:
        if (root / new).exists():
            raise ExtractError(f"{new} already exists")
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        must(run, ["git", "mv", old, new], cwd=root)
    if (root / APP / name).is_dir():
        _remove_leftovers(root / APP / name)
    return {new for _, new in pairs}


# -- configuration ----------------------------------------------------------------------------


def replace_once(text: str, old: str, new: str, *, what: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ExtractError(f"{what}: expected this exactly once, found it {count} times:\n{old}")
    return text.replace(old, new)


def _per_file_ignores(text: str) -> dict[str, list[str]]:
    lint = tomllib.loads(text).get("tool", {}).get("ruff", {}).get("lint", {})
    table: dict[str, list[str]] = lint.get("per-file-ignores", {})
    return table


def move_ignore_keys(text: str, names: Iterable[str]) -> str:
    """Per-file ignores follow their files: ``ddiver/app.py`` becomes the core path."""
    for key in _per_file_ignores(text):
        for name in names:
            for old, new in (
                (f"{APP}/{name}.py", f"{CORE_SRC}/{name}.py"),
                (f"{APP}/{name}/", f"{CORE_SRC}/{name}/"),
            ):
                if key == old or (old.endswith("/") and key.startswith(old)):
                    moved = new + key[len(old) :]
                    text = replace_once(text, f'"{key}" =', f'"{moved}" =', what="pyproject.toml")
    return text


def derive_ignores(split: Split | BaseSplit, text: str, *, root: Path, run: Runner) -> str:
    """Give a split-off module the source's per-file ignores that it still needs, and no more.

    The candidates are what the source file was allowed; the new module is checked with the
    project's configuration and keeps a selector only if ruff reports a code under it.
    """
    table = _per_file_ignores(text)
    candidates = list(
        dict.fromkeys(
            selector
            for pattern, selectors in table.items()
            if fnmatch.fnmatch(split.source, pattern)
            for selector in selectors
        )
    )
    out = must(
        run,
        ruff("check", "--exit-zero", "--output-format", "json", split.target_path),
        cwd=root,
    )
    codes = {str(item.get("code")) for item in json.loads(out or "[]")}
    needed = [s for s in candidates if any(code.startswith(s) for code in codes)]
    uncovered = sorted(c for c in codes if not any(c.startswith(s) for s in needed))
    if uncovered:
        raise ExtractError(
            f"{split.target_path} breaks {', '.join(uncovered)}, which {split.source} was not"
            " allowed to break either"
        )
    if not needed:
        return text
    header = "[tool.ruff.lint.per-file-ignores]\n"
    start = text.index(header)
    end = text.find("\n\n[", start)
    end = len(text.rstrip("\n")) if end < 0 else end
    selectors = ", ".join(json.dumps(s) for s in needed)
    entry = f'\n{_comment(split.ignores_reason)}"{split.target_path}" = [{selectors}]'
    return text[:end] + entry + text[end:]


CORE_PYPROJECT = """\
[project]
name = "ddiver-core"
version = "0.1.0"
description = "Telegram delivery and everything before it: rules, matching, storage, the bot UI."
license = "MIT"
requires-python = ">=3.12"
authors = [{ name = "vinter-man" }]
dependencies = [
  "telethon>=1.45,<2",
  "cryptg>=0.5",
  "aiosqlite>=0.21",
  "regex>=2024.11",
  "pydantic-settings>=2.6",
  "tzdata>=2025.1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ddiver_core"]
"""

CORE_INIT = '''\
"""ddiver-core: Telegram delivery and everything before it.

Rules and matching, storage, the bot UI and delivery, the supervisor, health, metrics and the
bot client. It talks to Telegram through Telethon, on the bot side only: no user session is
opened, read or written here. ddiver is one app on top of it (a Telethon listener); an app with
a different source reuses the rest unchanged. Nothing in here imports ``ddiver``.
"""
'''

DOCKERFILE_OLD = """\
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-install-project
COPY ddiver ./ddiver
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev
"""

DOCKERFILE_NEW = """\
# Dependencies first, from the lockfile and every workspace member's pyproject, so a code change
# does not reinstall Telethon. uv needs each member's pyproject to read the workspace at all,
# even while it installs none of them.
COPY pyproject.toml uv.lock README.md ./
COPY packages/ddiver-core/pyproject.toml ./packages/ddiver-core/
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-install-workspace
# Then the code. --no-editable installs ddiver and ddiver-core into the venv as ordinary
# packages, so what runs does not depend on where the sources sit.
COPY ddiver ./ddiver
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-editable
"""

CI_IMAGE_JOB = """\
  image:
    # A COPY that misses a workspace member breaks only the image, and nothing but the server
    # builds one. Build it here, so that deploy never ships a commit that cannot be built.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build the image
        run: docker build --build-arg GIT_SHA="$GITHUB_SHA" -t ddiver:ci .
      - name: Smoke-test the image
        # A build can succeed while the installed wheels miss a module (a hatch `packages`
        # setting that drops a subpackage) or the console script is broken, and the box would
        # only find out at its healthcheck, after `up -d`. The check job tests the editable
        # install; this imports every module of both packages from site-packages, as
        # production does, makes sure nothing of the dev group came along, and runs the entry
        # point, with no network and no credentials.
        run: |
          docker run --rm --network none --entrypoint python ddiver:ci -c '
          import importlib, importlib.util, pkgutil
          import ddiver, ddiver_core
          for package in (ddiver, ddiver_core):
              for found in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
                  importlib.import_module(found.name)
          dev_only = ("pytest",)
          shipped = [name for name in dev_only if importlib.util.find_spec(name)]
          assert not shipped, f"dev-only packages in the image: {shipped}"
          '
          docker run --rm --network none ddiver:ci --help

  deploy:
"""

PYPROJECT_EDITS = (
    ("dependencies = [\n", 'dependencies = [\n  "ddiver-core",\n'),
    (
        "\n[build-system]\n",
        (
            "\n[tool.uv.workspace]\n"
            'members = ["packages/ddiver-core"]\n\n'
            "[tool.uv.sources]\n"
            "ddiver-core = { workspace = true }\n\n"
            "[build-system]\n"
        ),
    ),
    ('src = ["ddiver", "tests"]', 'src = ["ddiver", "packages/ddiver-core/src", "tests"]'),
    ('known-first-party = ["ddiver"]', 'known-first-party = ["ddiver", "ddiver_core"]'),
    (
        'files = ["ddiver", "tests"]',
        'files = ["ddiver", "packages/ddiver-core/src", "tests", "tools"]',
    ),
    ('source = ["ddiver"]', 'source = ["ddiver", "ddiver_core"]'),
)

# pip ignores [tool.uv.sources] and would ask PyPI for "ddiver-core", where the name is not
# ours: installing the member by path first resolves it locally.
README_EDITS = (
    (
        "README.md",
        (
            (
                "uv sync                      # or: pip install -e .\n",
                "uv sync                      # or: pip install -e ./packages/ddiver-core -e .\n",
            ),
        ),
    ),
    (
        "README.ru.md",
        (
            (
                "uv sync                      # или: pip install -e .\n",
                "uv sync                      # или: pip install -e ./packages/ddiver-core -e .\n",
            ),
        ),
    ),
)


def _edit_file(root: Path, rel: str, edits: Iterable[tuple[str, str]]) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        text = replace_once(text, old, new, what=rel)
    path.write_text(text, encoding="utf-8")


def _check_toml(root: Path) -> None:
    try:
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ExtractError(f"pyproject.toml is not valid TOML after the edits: {exc}") from exc


def scaffold(root: Path, run: Runner) -> set[str]:
    """The workspace, an empty ddiver-core, and a build and a CI job that know about both."""
    init = root / CORE_SRC / "__init__.py"
    if init.exists():
        raise ExtractError(f"{CORE_SRC} already exists")
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(CORE_INIT, encoding="utf-8")
    (root / CORE_SRC / "py.typed").write_text("", encoding="utf-8")
    (root / CORE_ROOT / "pyproject.toml").write_text(CORE_PYPROJECT, encoding="utf-8")
    _edit_file(root, "pyproject.toml", PYPROJECT_EDITS)
    _check_toml(root)
    _edit_file(root, "Dockerfile", [(DOCKERFILE_OLD, DOCKERFILE_NEW)])
    _edit_file(root, ".dockerignore", [("scripts/\n", "scripts/\ntools/\n")])
    _edit_file(
        root,
        ".github/workflows/ci.yml",
        [("  deploy:\n", CI_IMAGE_JOB), ("    needs: check\n", "    needs: [check, image]\n")],
    )
    for rel, edits in README_EDITS:
        _edit_file(root, rel, edits)
    must(run, uv("lock"), cwd=root)
    must(run, uv("sync"), cwd=root)
    return {
        f"{CORE_SRC}/__init__.py",
        f"{CORE_SRC}/py.typed",
        f"{CORE_ROOT}/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        ".dockerignore",
        ".github/workflows/ci.yml",
        *(rel for rel, _ in README_EDITS),
    }


def add_member(member: Member, root: Path) -> set[str]:
    """Write a new workspace member and make the root pyproject, the image and CI know it.

    ``uv lock`` and ``uv sync`` run once the step's code is in place, since the member's
    package directory has to exist for uv to install it.
    """
    if (root / member.root).exists():
        raise ExtractError(f"{member.root} already exists")
    touched: set[str] = set()
    for rel, text in member.files:
        path = root / member.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        touched.add(f"{member.root}/{rel}")
    for rel, edits in member.edits:
        _edit_file(root, rel, edits)
        touched.add(rel)
    _check_toml(root)
    return touched


# -- the invariants ---------------------------------------------------------------------------


def verify(root: Path, reloc: Relocations) -> None:
    """The packages import nothing they must not, and a second rewrite changes nothing."""
    for src, banned in FORBIDDEN.items():
        for path in sorted((root / src).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in banned:
                        package = src.rpartition("/")[2]
                        raise ExtractError(
                            f"{rel}:{node.lineno} imports {name}; {package} must not"
                        )
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in NEVER_REWRITE:
            continue
        text = path.read_text(encoding="utf-8")
        if rewrite_source(text, view_for(rel, reloc), path=rel)[0] != text:
            raise ExtractError(f"{rel} still refers to a moved name after the rewrite")


# -- steps ------------------------------------------------------------------------------------


def is_applied(step: Step, root: Path) -> bool:
    core = root / CORE_SRC
    if step.moves:
        name = step.moves[0]
        return (core / f"{name}.py").is_file() or (core / name).is_dir()
    if step.splits:
        return (root / step.splits[0].target_path).is_file()
    return (core / "__init__.py").is_file()


@dataclass(frozen=True)
class Report:
    step: Step
    touched: tuple[str, ...]
    notes: tuple[str, ...]


def _change(step: Step, *, root: Path, run: Runner) -> set[str]:
    """The step's own edits, before anything is rewritten to follow them."""
    touched: set[str] = set()
    if step.key == "scaffold":
        touched |= scaffold(root, run)
    for member in step.members:
        touched |= add_member(member, root)
    for name in step.moves:
        touched |= move_module(name, root=root, run=run)
    for split in step.splits:
        touched |= apply_split(split, root)
    for base in step.bases:
        touched |= apply_base(base, root)
    return touched


def apply_step(key: str, *, root: Path, run: Runner, allow_dirty: bool = False) -> Report:
    index, step = step_named(key)
    for earlier in STEPS[:index]:
        if not is_applied(earlier, root):
            raise ExtractError(f"apply {earlier.key!r} first")
    if is_applied(step, root):
        raise ExtractError(f"{step.key!r} is already applied")
    dirty = must(run, ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root)
    if dirty.strip() and not allow_dirty:
        raise ExtractError("the working tree has uncommitted changes; commit or stash them")

    touched = _change(step, root=root, run=run)
    reloc = relocations_through(index, root)
    rewritten, notes = rewrite_tree(root, reloc)
    touched |= rewritten

    python = sorted(p for p in touched if p.endswith(".py") and (root / p).is_file())
    if python:
        must(run, ruff("check", "--fix", "--select", "I,F401", *python), cwd=root)
        must(run, ruff("format", *python), cwd=root)

    pyproject = root / "pyproject.toml"
    before = pyproject.read_text(encoding="utf-8")
    after = move_ignore_keys(before, step.moves)
    derived: tuple[Split | BaseSplit, ...] = (*step.splits, *step.bases)
    for split in derived:
        after = derive_ignores(split, after, root=root, run=run)
    if after != before:
        pyproject.write_text(after, encoding="utf-8")
        touched.add("pyproject.toml")
    if step.members:
        must(run, uv("lock"), cwd=root)
        must(run, uv("sync"), cwd=root)
        touched.add("uv.lock")

    verify(root, relocations_through(index, root))
    staged = sorted(p for p in touched if (root / p).exists())
    must(run, ["git", "add", "--", *staged], cwd=root)
    return Report(step, tuple(staged), tuple(notes))


CHECKS = (("ruff", "check", "."), ("ruff", "format", "--check", "."), ("mypy",), ("pytest", "-q"))


def check(*, root: Path, run: Runner) -> int:
    for args in CHECKS:
        result = run([sys.executable, "-m", *args], cwd=root)
        sys.stdout.write(result.out + result.err)
        if result.code != 0:
            return result.code
    return 0


def main(
    argv: Sequence[str] | None = None, *, run: Runner = run_subprocess, root: Path | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="extract_core", description=(__doc__ or "").split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="which steps are applied")
    apply = commands.add_parser("apply", help="apply one step and stage what it touched")
    apply.add_argument("step", choices=[step.key for step in STEPS])
    apply.add_argument("--allow-dirty", action="store_true", help="skip the clean-tree check")
    commands.add_parser("check", help="ruff, ruff format, mypy and pytest, as CI runs them")
    args = parser.parse_args(argv)

    root = root or Path.cwd()
    if not (root / "pyproject.toml").is_file() or not (root / APP).is_dir():
        sys.stderr.write("extract_core: run this from the root of the ddiver repository\n")
        return 2
    try:
        if args.command == "check":
            return check(root=root, run=run)
        if args.command == "status":
            for step in STEPS:
                mark = "applied" if is_applied(step, root) else "pending"
                sys.stdout.write(f"{mark:8} {step.key:9} {step.subject}\n")
            return 0
        report = apply_step(args.step, root=root, run=run, allow_dirty=args.allow_dirty)
    except ExtractError as exc:
        sys.stderr.write(f"extract_core: {exc}\n")
        return 1
    for note in report.notes:
        sys.stdout.write(f"rewrote {note}\n")
    sys.stdout.write(f"staged {len(report.touched)} paths. Commit subject:\n")
    sys.stdout.write(f"  {report.step.subject}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
