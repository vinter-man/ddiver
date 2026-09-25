"""Tiny dictionary-based i18n. ``t(lang, key, **kw)`` falls back to English, then to the key."""

from __future__ import annotations

from typing import Any

from ddiver_core.i18n import en, ru, uk

LANGS: tuple[str, ...] = ("uk", "ru", "en")
DEFAULT_LANG = "en"
LANG_NAMES: dict[str, str] = {"uk": "Українська", "ru": "Русский", "en": "English"}

_CATALOGS: dict[str, dict[str, str]] = {"en": en.STRINGS, "ru": ru.STRINGS, "uk": uk.STRINGS}


def t(lang: str, key: str, /, **kw: Any) -> str:
    template = _CATALOGS.get(lang, {}).get(key) or _CATALOGS[DEFAULT_LANG].get(key) or key
    if kw:
        try:
            return template.format(**kw)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def missing_keys() -> dict[str, list[str]]:
    """Keys present in English but missing elsewhere (used by a test to keep catalogs in sync)."""
    base = set(_CATALOGS[DEFAULT_LANG])
    return {
        lang: sorted(base - set(cat)) for lang, cat in _CATALOGS.items() if lang != DEFAULT_LANG
    }
