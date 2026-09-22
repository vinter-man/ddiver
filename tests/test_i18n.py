from ddiver.i18n import LANGS, missing_keys, t


def test_all_catalogs_have_every_english_key():
    assert missing_keys() == {lang: [] for lang in LANGS if lang != "en"}


def test_fallbacks():
    assert t("ru", "rule.err.empty") == "Пустой шаблон."
    assert t("xx", "rule.err.empty") == "Empty pattern."
    assert t("en", "no.such.key") == "no.such.key"
    assert t("en", "rule.err.too_long", limit=3) == "Pattern is longer than 3 characters."
    # bad format args never raise
    assert t("en", "rule.err.too_long", wrong=1) == "Pattern is longer than {limit} characters."
