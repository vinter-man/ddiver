"""Tiny dictionary-based i18n. ``t(lang, key, **kw)`` falls back to English, then to the key.

An app built on the core can lay its own strings over the base catalogs with
:func:`register_overlay`: ddiver-fb speaks of groups and posts where ddiver speaks of chats and
messages, and it has keys of its own. A lookup tries the overlay and then the base catalog of
the user's language, then the same two for English, then gives back the key. The user's
language comes first on purpose: a sentence in their language with the base wording beats
the right wording in a language they may not read, and the catalog-sync test is what keeps
that case from happening. ddiver registers no overlay.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from typing import Any

from ddiver_core.i18n import en, ru, uk

LANGS: tuple[str, ...] = ("uk", "ru", "en")
DEFAULT_LANG = "en"
LANG_NAMES: dict[str, str] = {"uk": "Українська", "ru": "Русский", "en": "English"}

_CATALOGS: dict[str, dict[str, str]] = {"en": en.STRINGS, "ru": ru.STRINGS, "uk": uk.STRINGS}
_OVERLAYS: dict[str, dict[str, str]] = {}
_CONVERSIONS = frozenset({None, "r", "s", "a"})


def _lookup(lang: str, key: str) -> str | None:
    return _OVERLAYS.get(lang, {}).get(key) or _CATALOGS.get(lang, {}).get(key)


def t(lang: str, key: str, /, **kw: Any) -> str:
    template = _lookup(lang, key) or _lookup(DEFAULT_LANG, key) or key
    if kw:
        try:
            return template.format(**kw)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def register_overlay(lang: str, catalog: Mapping[str, object]) -> None:
    """Lay ``catalog`` over the base strings of ``lang``; a later call wins key by key.

    Checked here, at startup, rather than discovered by a user: the language must be one the
    bot offers (``ua`` for ``uk`` would otherwise be an overlay nobody ever sees), a string
    must not be empty (``t`` would quietly fall back to the base wording), and a string that
    replaces a base one may only use the placeholders the base one has, because those are the
    only values the code passes — anything else would reach the user as a literal ``{name}``.
    """
    if lang not in LANGS:
        msg = f"no such language for an overlay: {lang!r} (known: {', '.join(LANGS)})"
        raise ValueError(msg)
    checked: dict[str, str] = {}
    for key, text in catalog.items():
        if not isinstance(text, str):
            msg = f"overlay {lang}: {key} is not text"
            raise TypeError(msg)
        if not text:
            msg = f"overlay {lang}: {key} is empty"
            raise ValueError(msg)
        used = _placeholders(text)
        if used is None:
            msg = f"overlay {lang}: {key} is not a valid template (a literal brace is {{{{)"
            raise ValueError(msg)
        base = _CATALOGS[DEFAULT_LANG].get(key)
        allowed = _placeholders(base) if base is not None else None
        if allowed is not None and not used <= allowed:
            extra = ", ".join(sorted(used - allowed))
            msg = f"overlay {lang}: {key} uses placeholders the code never passes: {extra}"
            raise ValueError(msg)
        checked[key] = text
    _OVERLAYS.setdefault(lang, {}).update(checked)


def clear_overlays() -> None:
    """Forget every overlay. For tests, and for an app that reconfigures in-process."""
    _OVERLAYS.clear()


def missing_keys() -> dict[str, list[str]]:
    """Keys English has but another language lacks, overlays included on both sides.

    Used by a test to keep catalogs in sync. An app's overlay that adds a key in English only
    shows up here as missing in the other languages.
    """
    reference = _keys(DEFAULT_LANG)
    return {lang: sorted(reference - _keys(lang)) for lang in _CATALOGS if lang != DEFAULT_LANG}


def _keys(lang: str) -> set[str]:
    return set(_CATALOGS.get(lang, {})) | set(_OVERLAYS.get(lang, {}))


def _placeholders(template: str) -> set[str] | None:
    """Field names a template uses, or ``None`` when it is malformed.

    The English base string of a key decides which names the code passes, since every call
    site was written against it.
    """
    try:
        return _fields(template, nested=False)
    except ValueError:
        return None


def _fields(template: str, *, nested: bool) -> set[str]:
    """Every field name in *template*, including those nested in a format spec (``{n:{w}}``).

    Raises ``ValueError`` for anything ``t()`` could not fill: a malformed template, an unknown
    conversion (``parse()`` lets ``{n!z}`` through; ``format()`` then raises), a positional
    field (``t()`` passes keywords only) or nesting deeper than ``format()`` allows.
    """
    names: set[str] = set()
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if conversion not in _CONVERSIONS:
            msg = f"unknown conversion {conversion!r}"
            raise ValueError(msg)
        name = field.split(".", 1)[0].split("[", 1)[0]
        if not name or name.isdigit():
            msg = "positional field"
            raise ValueError(msg)
        names.add(name)
        if spec:
            if nested:
                msg = "format spec nested too deeply"
                raise ValueError(msg)
            names |= _fields(spec, nested=True)
    return names
