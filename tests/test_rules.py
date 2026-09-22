import pytest
import regex

from ddiver.rules import (
    MAX_HIGHLIGHTS,
    MAX_PATTERN_LEN,
    CompiledRule,
    MatchMode,
    RuleError,
    RuleTimeout,
    Span,
    clean_text,
    compile_rule,
    normalize_pattern,
    parse_pattern,
    parse_rule_lines,
    pattern_key,
    prepare_text,
)

T = 0.2


@pytest.mark.parametrize(
    ("raw", "mode", "payload"),
    [
        ("казино", MatchMode.SUBSTRING, "казино"),
        ("  free money ", MatchMode.SUBSTRING, "free money"),
        ("=бан", MatchMode.WORD, "бан"),
        ("= spaced ", MatchMode.WORD, "spaced"),
        ("re:\\d{3}", MatchMode.REGEX, "\\d{3}"),
        ("re: x ", MatchMode.REGEX, "x"),
    ],
)
def test_parse_pattern(raw, mode, payload):
    assert parse_pattern(raw) == (mode, payload)


def test_normalize_keeps_prefix_and_trims():
    assert normalize_pattern("  =Word ") == "=Word"
    assert normalize_pattern("re:  a+ ") == "re:a+"
    assert normalize_pattern(" plain ") == "plain"


class TestSubstring:
    def test_case_insensitive_cyrillic(self):
        rule = compile_rule("Спам", timeout=T)
        assert rule.search("это СПАМ!", T) is not None
        assert rule.search("спам", T) is not None
        assert rule.search("спа м", T) is None

    def test_substring_matches_inside_word(self):
        rule = compile_rule("бан", timeout=T)
        span = rule.search("его забанили", T)
        assert span is not None
        assert "его забанили"[span.start : span.end] == "бан"

    def test_regex_metachars_are_literal(self):
        rule = compile_rule("24/7 (срочно)", timeout=T)
        assert rule.search("работаем 24/7 (срочно)", T) is not None
        assert rule.search("работаем 2447 срочно", T) is None

    def test_yo_and_ye_are_not_folded(self):
        rule = compile_rule("ещё", timeout=T)
        assert rule.search("ЕЩЁ", T) is not None
        assert rule.search("еще", T) is None


class TestWord:
    def test_whole_word_only(self):
        rule = compile_rule("=бан", timeout=T)
        assert rule.search("дать бан всем", T) is not None
        assert rule.search("Бан!", T) is not None
        assert rule.search("забанили", T) is None
        assert rule.search("банан", T) is None

    def test_punctuation_words(self):
        assert compile_rule("=c++", timeout=T).search("learn c++ now", T) is not None
        assert compile_rule("=c++", timeout=T).search("abc++", T) is None
        assert compile_rule("=#tag", timeout=T).search("see #tag.", T) is not None

    def test_word_at_string_edges(self):
        rule = compile_rule("=x", timeout=T)
        assert rule.search("x", T) is not None
        assert rule.search("x y", T) is not None
        assert rule.search("xy", T) is None


class TestRegex:
    def test_ignorecase_by_default_and_opt_out(self):
        assert compile_rule("re:abc", timeout=T).search("ABC", T) is not None
        assert compile_rule("re:(?-i:abc)", timeout=T).search("ABC", T) is None

    def test_invalid_regex(self):
        with pytest.raises(RuleError) as ei:
            compile_rule("re:(unclosed", timeout=T)
        assert ei.value.key == "rule.err.invalid_regex"

    @pytest.mark.parametrize("raw", ["re:.*", "re:^", "re:a?", "re:$", "re:(?:)"])
    def test_regex_matching_empty_is_rejected(self, raw):
        with pytest.raises(RuleError) as ei:
            compile_rule(raw, timeout=T)
        assert ei.value.key == "rule.err.matches_empty"

    def test_catastrophic_regex_rejected_at_compile(self):
        with pytest.raises(RuleError) as ei:
            compile_rule("re:((a|aa)+)+b", timeout=0.05)
        assert ei.value.key == "rule.err.too_slow"

    def test_timeout_at_search_raises_rule_timeout(self):
        # Validation would reject this pattern, so build the compiled rule directly.
        rule = CompiledRule("re:((a|aa)+)+b", MatchMode.REGEX, regex.compile("((a|aa)+)+b"))
        with pytest.raises(RuleTimeout):
            rule.search("a" * 40, 0.02)

    def test_span_is_returned(self):
        rule = compile_rule("re:\\d{2,}", timeout=T)
        span = rule.search("tel 12345 now", T)
        assert span is not None
        assert "tel 12345 now"[span.start : span.end] == "12345"


class TestValidation:
    @pytest.mark.parametrize("raw", ["", "   ", "=", "re:", "=  ", "re:   "])
    def test_empty(self, raw):
        with pytest.raises(RuleError) as ei:
            compile_rule(raw, timeout=T)
        assert ei.value.key == "rule.err.empty"

    def test_too_long(self):
        with pytest.raises(RuleError) as ei:
            compile_rule("x" * (MAX_PATTERN_LEN + 1), timeout=T)
        assert ei.value.key == "rule.err.too_long"
        assert ei.value.params["limit"] == MAX_PATTERN_LEN

    def test_pattern_is_stored_normalized(self):
        assert compile_rule("  =Word  ", timeout=T).pattern == "=Word"


def test_parse_rule_lines_keeps_every_non_blank_line():
    text = "казино\n\n#промо\n  =бан  \nre:\\d+\n   "
    assert parse_rule_lines(text) == ["казино", "#промо", "=бан", "re:\\d+"]


class TestCleaningAndPreparedText:
    def test_clean_text_strips_format_chars_and_normalizes(self):
        cleaned, removed = clean_text("ка\u200bзи\ufeffно")
        assert (cleaned, removed) == ("казино", 2)
        nfd = "и\u0306"  # й as base + combining breve
        assert clean_text(nfd) == ("й", 0)

    def test_normalize_pattern_cleans(self):
        assert normalize_pattern("  =ка\u200bзино ") == "=казино"

    def test_pattern_key(self):
        assert pattern_key("Казино") == pattern_key("казино") == "казино"
        assert pattern_key("=Бан") == "=бан"
        assert pattern_key("re:Abc") == "re:Abc"
        assert pattern_key("re:Abc") != pattern_key("re:abc")

    def test_prepare_text_identity_fast_path(self):
        pt = prepare_text("plain казино")
        assert pt.shadow == "plain казино"
        assert pt.to_original(Span(6, 12)) == Span(6, 12)

    def test_prepare_text_maps_spans_through_zero_width_chars(self):
        text = "spam ка\u200bзи\u200cно!"
        pt = prepare_text(text)
        assert pt.shadow == "spam казино!"
        rule = compile_rule("казино", timeout=T)
        span = rule.search(pt.shadow, T)
        assert span is not None
        orig = pt.to_original(span)
        assert text[orig.start : orig.end] == "ка\u200bзи\u200cно"

    def test_prepare_text_composes_nfd_sequences(self):
        text = "x й\u0306y"  # NFD й in the middle
        text = "x и\u0306 y"
        pt = prepare_text(text)
        assert pt.shadow == "x й y"
        span = compile_rule("й", timeout=T).search(pt.shadow, T)
        assert span is not None
        orig = pt.to_original(span)
        assert text[orig.start : orig.end] == "и\u0306"

    def test_zero_length_regex_match_is_skipped(self):
        rule = CompiledRule("re:x*", MatchMode.REGEX, regex.compile("x*"))
        assert rule.search("abc", T) is None
        assert rule.search("abxxc", T) == Span(2, 4)

    def test_search_all_returns_several_spans(self):
        rule = compile_rule("спам", timeout=T)
        text = "спам, СПАМ и ещё спам"
        spans = rule.search_all(text, T)
        assert [text[s.start : s.end] for s in spans] == ["спам", "СПАМ", "спам"]
        assert rule.search_all(text, T, limit=2) == spans[:2]

    def test_search_all_caps_at_max_highlights(self):
        rule = compile_rule("x", timeout=T)
        assert len(rule.search_all("x" * 50, T)) == MAX_HIGHLIGHTS

    def test_hashtag_lines_are_rules_not_comments(self):
        assert parse_rule_lines("#промо\r\n=#tag\r\n\r\n") == ["#промо", "=#tag"]
        assert compile_rule("#промо", timeout=T).search("see #ПРОМО now", T) is not None
