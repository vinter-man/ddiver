"""An app lays its own strings over the base catalogs without touching them.

ddiver-fb says "group" where ddiver says "chat" and adds keys of its own; ddiver registers
nothing and must read exactly what it always read.
"""

import subprocess
import sys
from collections.abc import Iterator

import pytest

from ddiver_core import i18n
from ddiver_core.i18n import LANGS, clear_overlays, missing_keys, register_overlay, t
from ddiver_core.i18n import en as en_catalog


@pytest.fixture(autouse=True)
def no_overlay_leaks() -> Iterator[None]:
    """Overlays are process-wide; one left behind would change every later test's strings."""
    clear_overlays()
    yield
    clear_overlays()


class TestLookup:
    def test_an_overlay_wins_over_the_base_catalog_of_its_language(self):
        register_overlay("en", {"notif.chat": "📡 Group"})
        assert t("en", "notif.chat") == "📡 Group"
        assert t("ru", "notif.chat") == i18n.ru.STRINGS["notif.chat"]  # other languages untouched

    def test_a_new_key_is_found_in_its_language_and_through_the_english_fallback(self):
        register_overlay("en", {"fb.collector.ok": "Collector: OK"})
        register_overlay("uk", {"fb.collector.ok": "Колектор: OK"})
        assert t("uk", "fb.collector.ok") == "Колектор: OK"
        assert t("ru", "fb.collector.ok") == "Collector: OK"  # no ru overlay → English overlay
        assert t("xx", "fb.collector.ok") == "Collector: OK"

    def test_the_users_language_comes_before_an_english_overlay(self):
        """Base wording in the user's language beats the right wording in English."""
        register_overlay("en", {"notif.chat": "📡 Group"})
        assert t("uk", "notif.chat") == i18n.uk.STRINGS["notif.chat"]

    def test_an_overlay_formats_with_the_same_arguments(self):
        register_overlay("en", {"bot.chats.title": "Groups you watch ({n})."})
        assert t("en", "bot.chats.title", n=3) == "Groups you watch (3)."

    def test_a_later_registration_wins_key_by_key(self):
        register_overlay("en", {"notif.chat": "A", "notif.link": "B"})
        register_overlay("en", {"notif.chat": "C"})
        assert t("en", "notif.chat") == "C"
        assert t("en", "notif.link") == "B"

    def test_the_registered_mapping_is_copied(self):
        strings = {"notif.chat": "📡 Group"}
        register_overlay("en", strings)
        strings["notif.chat"] = "changed later"
        assert t("en", "notif.chat") == "📡 Group"

    def test_clearing_restores_the_base_strings(self):
        register_overlay("en", {"notif.chat": "📡 Group"})
        clear_overlays()
        assert t("en", "notif.chat") == en_catalog.STRINGS["notif.chat"]

    def test_unknown_keys_still_come_back_as_the_key(self):
        register_overlay("en", {"notif.chat": "📡 Group"})
        assert t("en", "no.such.key") == "no.such.key"


class TestValidation:
    @pytest.mark.parametrize("lang", ["ua", "de", "EN", ""])
    def test_a_language_the_bot_does_not_offer_is_refused(self, lang):
        with pytest.raises(ValueError, match="no such language"):
            register_overlay(lang, {"notif.chat": "x"})

    def test_an_empty_string_is_refused(self):
        """``t`` treats an empty string as missing, so it would silently show the base text."""
        with pytest.raises(ValueError, match="empty"):
            register_overlay("en", {"notif.chat": ""})

    def test_a_value_that_is_not_text_is_refused(self):
        with pytest.raises(TypeError, match="not text"):
            register_overlay("en", {"notif.chat": 5})

    def test_a_placeholder_the_code_never_passes_is_refused(self):
        with pytest.raises(ValueError, match="count"):
            register_overlay("en", {"bot.chats.title": "Groups ({count})"})

    @pytest.mark.parametrize("text", ["Группы ({})", "Группы ({0})", "Группы ({n:{}})"])
    def test_a_positional_placeholder_is_refused_since_t_passes_names_only(self, text):
        with pytest.raises(ValueError, match="not a valid template"):
            register_overlay("ru", {"bot.chats.title": text})

    @pytest.mark.parametrize("text", ["Groups ({n:{width}})", "Groups ({n:.{precision}f})"])
    def test_a_name_nested_in_a_format_spec_is_checked_too(self, text):
        # parse() reports only the outer field; the inner one would reach the user as braces.
        with pytest.raises(ValueError, match="never passes"):
            register_overlay("en", {"bot.chats.title": text})

    def test_a_nested_name_the_code_passes_is_fine(self):
        register_overlay("en", {"bot.chats.title": "Groups ({n:{n}})"})
        assert t("en", "bot.chats.title", n=3) == "Groups (  3)"

    def test_nesting_deeper_than_format_allows_is_refused(self):
        with pytest.raises(ValueError, match="not a valid template"):
            register_overlay("en", {"bot.chats.title": "Groups ({n:{n:{n}}})"})

    def test_fewer_placeholders_than_the_base_are_fine(self):
        register_overlay("en", {"bot.chats.title": "Your groups"})
        assert t("en", "bot.chats.title", n=3) == "Your groups"

    def test_a_format_spec_does_not_hide_the_name(self):
        register_overlay("en", {"bot.chats.title": "Groups ({n:>3})"})
        assert t("en", "bot.chats.title", n=7) == "Groups (  7)"

    @pytest.mark.parametrize("text", ["Groups {n", "Groups }", "{n!z}"])
    def test_a_malformed_template_is_refused(self, text):
        with pytest.raises(ValueError, match="not a valid template"):
            register_overlay("en", {"bot.chats.title": text})

    def test_a_new_key_may_use_any_names(self):
        register_overlay("en", {"fb.status.blocked": "Blocked until {until}: {reason}"})
        assert t("en", "fb.status.blocked", until="12:00", reason="x") == "Blocked until 12:00: x"

    def test_a_malformed_new_key_is_refused_too(self):
        with pytest.raises(ValueError, match="not a valid template"):
            register_overlay("en", {"fb.status.blocked": "Blocked {"})

    def test_a_refused_overlay_registers_nothing(self):
        with pytest.raises(ValueError, match="empty"):
            register_overlay("en", {"notif.chat": "📡 Group", "notif.link": ""})
        assert t("en", "notif.chat") == en_catalog.STRINGS["notif.chat"]

    def test_every_base_string_is_a_template_the_check_can_read(self):
        """Otherwise the placeholder check would silently skip that key."""
        for lang in LANGS:
            for key, text in i18n._CATALOGS[lang].items():
                assert i18n._placeholders(text) is not None, (lang, key)


class TestMissingKeys:
    def test_without_overlays_the_catalogs_are_in_sync(self):
        assert missing_keys() == {lang: [] for lang in LANGS if lang != "en"}

    def test_a_key_added_in_english_only_is_missing_elsewhere(self):
        register_overlay("en", {"fb.groups.refresh": "🔄 Refresh groups"})
        assert missing_keys() == {"uk": ["fb.groups.refresh"], "ru": ["fb.groups.refresh"]}

    def test_a_complete_overlay_is_in_sync(self):
        for lang in LANGS:
            register_overlay(lang, {"fb.groups.refresh": f"🔄 {lang}"})
        assert missing_keys() == {"uk": [], "ru": []}

    def test_overriding_a_base_key_in_one_language_changes_nothing(self):
        register_overlay("ru", {"notif.chat": "📡 Group"})
        assert missing_keys() == {"uk": [], "ru": []}


def test_ddiver_registers_no_overlay():
    """Importing every module of the Telegram app leaves the catalogs exactly as they are.

    In a fresh interpreter: in this one the modules are long imported, and the fixture above
    has already cleared whatever an import might have registered.
    """
    probe = (
        "import importlib, pkgutil, ddiver\n"
        "for found in pkgutil.walk_packages(ddiver.__path__, 'ddiver.'):\n"
        "    importlib.import_module(found.name)\n"
        "from ddiver_core import i18n\n"
        "assert i18n._OVERLAYS == {}, sorted(i18n._OVERLAYS)\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter running a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
