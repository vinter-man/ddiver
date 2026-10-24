"""Nothing in the working tree may look like a live credential.

GitHub's secret scanning is the backstop, and it only speaks after a push: by then the value is
in a public history and revoking it is the only cure. This runs in the gate instead, before the
commit exists — which is also the moment it is still cheap.

It fires on shape, not on provenance, so it cannot tell a real token from a convincing
placeholder. That is deliberate: the fix for both is the same, and a placeholder that trips a
scanner costs somebody an afternoon proving it was nothing. Test fixtures therefore use a token
that passes ``Settings._bot_token_shape`` without matching any detector — digits, a colon, and
no ``AA`` prefix.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: What the scanners look for, in the formats this project could plausibly leak.
PATTERNS = {
    # @BotFather: <bot id>:AA<35 chars>. The AA is what makes it unmistakable.
    "Telegram bot token": re.compile(r"\b\d{5,16}:AA[\w-]{30,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY"),
    # api_hash is 32 hex characters and is as good as the account it belongs to.
    "Telegram api_hash": re.compile(r"\bAPI_HASH\s*[=:]\s*['\"]?[0-9a-f]{32}\b"),
}


def tracked_text_files() -> list[Path]:
    """Only what git would publish; the working tree also holds .env and the session."""
    out = subprocess.run(  # noqa: S603 - git with fixed arguments
        ["git", "-C", str(ROOT), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [ROOT / name for name in out.split("\0") if name]


@pytest.mark.parametrize("label", sorted(PATTERNS))
def test_no_tracked_file_carries_one(label: str) -> None:
    pattern = PATTERNS[label]
    offenders = []
    for path in tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue  # a binary fixture, or a file git knows about and the tree does not
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        f"{label} found in {offenders}. If it is real, revoke it — it is already in the "
        f"index. If it is a fixture, give it a shape no scanner claims."
    )


def test_the_fixture_token_still_satisfies_the_config_validator() -> None:
    """The placeholder has two jobs and it is easy to fix one by breaking the other."""
    from ddiver_core.config import Settings

    token = "123456789:NOT-A-REAL-BOTFATHER-SECRET"
    digits, sep, rest = token.partition(":")
    assert sep and digits.isdigit() and len(rest) >= 20, "would fail _bot_token_shape"
    assert not PATTERNS["Telegram bot token"].search(token), "would trip secret scanning"
    assert Settings.__name__  # the validator this mirrors lives there


def test_the_patterns_match_what_they_claim_to() -> None:
    """A guard whose regex never matches anything passes forever and protects nothing.

    The samples are built by concatenation on purpose: written as literals they would sit in
    this file looking exactly like the thing the test above refuses to allow.
    """
    token = "123456789" + ":" + "AA" + "F" * 33
    key = "-----BEGIN" + " OPENSSH PRIVATE KEY" + "-----"
    api_hash = "API_HASH=" + "0123456789abcdef" * 2

    assert PATTERNS["Telegram bot token"].search(token)
    assert PATTERNS["private key block"].search(key)
    assert PATTERNS["Telegram api_hash"].search(api_hash)

    # and that they do not fire on the shapes this repository does ship
    assert not PATTERNS["Telegram bot token"].search("123456789:NOT-A-REAL-BOTFATHER-SECRET")
    assert not PATTERNS["Telegram bot token"].search("BOT_TOKEN=123456789:AA...")
    assert not PATTERNS["Telegram api_hash"].search("API_HASH=your-api-hash-here")
