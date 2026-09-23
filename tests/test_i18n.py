from ddiver.i18n import LANGS, missing_keys, t
from ddiver.i18n import en as en_catalog


def test_all_catalogs_have_every_english_key():
    assert missing_keys() == {lang: [] for lang in LANGS if lang != "en"}


def test_fallbacks():
    assert t("ru", "rule.err.empty") == "Пустой шаблон."
    assert t("xx", "rule.err.empty") == "Empty pattern."
    assert t("en", "no.such.key") == "no.such.key"
    assert t("en", "rule.err.too_long", limit=3) == "Pattern is longer than 3 characters."
    # bad format args never raise
    assert t("en", "rule.err.too_long", wrong=1) == "Pattern is longer than {limit} characters."


def test_the_english_catalog_is_readable_to_an_english_speaker():
    """Rule examples used to be spelled in Cyrillic even in the English texts."""
    cyrillic = [
        key for key, text in en_catalog.STRINGS.items() if any("Ѐ" <= ch <= "ӿ" for ch in str(text))
    ]
    assert cyrillic == []


HTML_KEYS = {"bot.help", "bot.rules.prompt", "bot.test.prompt"}


def test_html_strings_parse_and_nothing_else_carries_tags():
    """A tag in a string sent without parse_mode would show up literally to the user."""
    from telethon.extensions import html

    for lang in LANGS:
        for key, text in _catalog(lang).items():
            if key in HTML_KEYS:
                rendered, entities = html.parse(text)
                assert "<" not in rendered, (lang, key)
                assert entities, (lang, key)
            else:
                assert "<b>" not in text and "<code>" not in text, (lang, key)


def _catalog(lang):
    from ddiver.i18n import en as en_mod
    from ddiver.i18n import ru as ru_mod
    from ddiver.i18n import uk as uk_mod

    return {"en": en_mod, "ru": ru_mod, "uk": uk_mod}[lang].STRINGS
