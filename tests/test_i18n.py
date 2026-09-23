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
