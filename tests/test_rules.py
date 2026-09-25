import tracemalloc

import pytest
import regex

from ddiver.rules import (
    MAX_COMPILED_BYTES,
    MAX_HIGHLIGHTS,
    MAX_PATTERN_LEN,
    MAX_REPEAT,
    MAX_UNROLL,
    CompiledRule,
    MatchMode,
    RuleError,
    RuleTimeout,
    SenderField,
    Span,
    _unroll_cost,
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


class TestRepeatGuard:
    """What compiling a pattern costs, as opposed to how long the pattern is.

    `re:a{2000000}` is thirteen characters and allocates about half a gigabyte before anything
    can be timed. The first guard against it multiplied every bounded repeat by every other,
    which also refused patterns moderators actually write — and auto-disabled such rules
    already stored, telling their owner they would "use too much memory" when the largest
    repetition in them was fifty. So both halves are asserted here: what must be refused, and
    what must not.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            r"\w{2,32}@\w{2,32}\.\w{2,8}",  # somebody sharing an e-mail address
            r".{50}такси.{50}",  # a word with a gap either side
            r"куплю.{0,40}аккаунт.{0,40}telegram",  # a resale ad
            r"[a-z]{100}[0-9]{100}",  # two classes in a row
            r".{999}",  # flat, and right at the single-repeat limit
        ],
    )
    def test_ordinary_patterns_are_accepted(self, pattern):
        """Measured: each of these compiles in under 2 ms and under 240 KiB."""
        assert compile_rule(f"re:{pattern}", timeout=T).mode is MatchMode.REGEX

    @pytest.mark.parametrize(
        ("pattern", "key"),
        [
            (r"a{2000000}", "rule.err.repeat_too_big"),  # one count, no nesting
            (r"(a{1000}){1000}", "rule.err.unrolls_too_big"),  # a million atoms
            (r"(a{500}){500}", "rule.err.unrolls_too_big"),  # neither count is over the limit
            (r"(.{100}){999}", "rule.err.unrolls_too_big"),
            (r"(\w{32}){300}", "rule.err.unrolls_too_big"),  # 9600 atoms, 2.7 MB, 29 ms
        ],
    )
    def test_expensive_patterns_are_refused(self, pattern, key):
        with pytest.raises(RuleError) as ei:
            compile_rule(f"re:{pattern}", timeout=T)
        assert ei.value.key == key

    def test_the_bomb_is_refused_before_it_is_compiled(self):
        """The point of the guard: the memory is allocated by `regex.compile` itself."""
        tracemalloc.start()
        try:
            with pytest.raises(RuleError):
                compile_rule("re:a{2000000}", timeout=T)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 5 * 1024 * 1024, f"{peak / 1e6:.1f} MB — it compiled it"

    def test_a_group_carries_its_cost_into_the_repeat_that_follows(self):
        """`(ab){10}` is twenty atoms, not two and not ten."""
        assert _unroll_cost("(ab){10}") == 20
        assert _unroll_cost("ab{10}") == 11  # only `b` repeats
        assert _unroll_cost("a{10}b{10}") == 20  # siblings add, they do not multiply

    def test_a_zero_repeat_costs_nothing(self):
        assert _unroll_cost("a{0}") == 0

    def test_a_brace_that_is_not_a_repeat_is_just_a_character(self):
        assert _unroll_cost("{foo}") == 5

    def test_a_class_is_one_atom_however_it_is_written(self):
        assert _unroll_cost("[]}{a-z]") == 1  # a leading ] is a literal
        assert _unroll_cost(r"[\]]") == 1

    def test_the_limits_are_the_ones_the_message_quotes(self):
        with pytest.raises(RuleError) as ei:
            compile_rule("re:a{2000000}", timeout=T)
        assert ei.value.params["limit"] == MAX_REPEAT
        with pytest.raises(RuleError) as ei:
            compile_rule("re:(a{1000}){1000}", timeout=T)
        assert ei.value.params["limit"] == MAX_UNROLL


class TestCompiledSizeGuard:
    """What a pattern *costs*, as opposed to how many pieces it has.

    MAX_UNROLL counts atoms, and an atom is not a fixed size. A wide Unicode class is about
    eight times a plain one, so a pattern could sit inside every stated limit — 217 of 300
    characters, 4993 of 5000 atoms — and still allocate 4.6 MB. A hundred of them is a dead
    container, and since rules live in SQLite the next start compiles them again before it can
    disable anything: a crash loop installable with one /import.
    """

    HEAVY = (
        r"q9q"
        + r"[\p{L}\p{N}\p{P}\p{S}\p{M}\p{Z}\p{C}]{1000}" * 4
        + r"[\p{L}\p{N}\p{P}\p{S}\p{M}\p{Z}\p{C}]{990}"
    )

    def test_the_heavy_pattern_passes_every_other_check(self):
        """Which is the point: nothing before this guard had any reason to refuse it."""
        assert len(self.HEAVY) <= MAX_PATTERN_LEN
        assert _unroll_cost(self.HEAVY) <= MAX_UNROLL

    def test_and_is_refused_anyway(self):
        with pytest.raises(RuleError) as ei:
            compile_rule(f"re:{self.HEAVY}", timeout=T)
        assert ei.value.key == "rule.err.unrolls_too_big"

    def test_refusing_it_does_not_leave_the_memory_behind(self):
        """The module caches compiled patterns, so dropping our reference frees nothing.

        Rejecting a hundred of these in one import was itself a way to exhaust the container.
        """
        regex.purge()
        tracemalloc.start()
        try:
            for i in range(6):
                with pytest.raises(RuleError):
                    compile_rule(f"re:q{i}q{self.HEAVY[3:]}", timeout=T)
            held = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        assert held < 6 * MAX_COMPILED_BYTES, f"{held / 1e6:.1f} MB still held"

    @pytest.mark.parametrize(
        "pattern",
        [r".{999}", r".{50}такси.{50}", r"\w{2,32}@\w{2,32}\.\w{2,8}", r"[а-яё]{40}"],
    )
    def test_ordinary_patterns_are_far_below_the_cap(self, pattern):
        """Measured: the largest of these is 119 KB against a 512 KB cap."""
        rule = compile_rule(f"re:{pattern}", timeout=T)
        assert 0 < rule.cost_bytes < MAX_COMPILED_BYTES

    def test_a_plain_keyword_costs_almost_nothing(self):
        assert compile_rule("такси", timeout=T).cost_bytes < 32 * 1024


class TestSenderRules:
    """`from:` watches who sent a message, which the other three modes cannot do."""

    @pytest.mark.parametrize(
        ("raw", "field", "value"),
        [
            ("from:@vasya_spam", SenderField.USERNAME, "vasya_spam"),
            ("from:@VasyaSpam", SenderField.USERNAME, "vasyaspam"),  # exact, but not cased
            ("FROM:@vasya_spam", SenderField.USERNAME, "vasya_spam"),  # the prefix is not cased
            ("from:123456789", SenderField.ID, "123456789"),
            ("from:-1001234567890", SenderField.ID, "-1001234567890"),  # a channel writing
            ("from:Вася Пупкин", SenderField.NAME, "вася пупкин"),
            ("from:  Вася   Пупкин  ", SenderField.NAME, "вася пупкин"),  # whitespace folded
        ],
    )
    def test_what_each_shape_parses_to(self, raw, field, value):
        rule = compile_rule(raw, timeout=T)
        assert rule.mode is MatchMode.SENDER
        assert rule.target is not None
        assert rule.target.field is field
        assert rule.target.value == value

    @pytest.mark.parametrize(
        ("raw", "key"),
        [
            ("from:+380671234567", "rule.err.no_phone"),
            ("from:+1 555 0100", "rule.err.no_phone"),
            ("from:@ab", "rule.err.bad_username"),
            ("from:@" + "x" * 33, "rule.err.bad_username"),
            ("from:@вася", "rule.err.bad_username"),  # Telegram usernames are ASCII
            ("from:ан", "rule.err.name_too_short"),
            ("from:", "rule.err.empty"),
            ("from:   ", "rule.err.empty"),
            (f"from:{2**64}", "rule.err.bad_sender_id"),
        ],
    )
    def test_what_is_refused_and_why(self, raw, key):
        with pytest.raises(RuleError) as ei:
            compile_rule(raw, timeout=T)
        assert ei.value.key == key

    def test_a_sender_rule_never_searches_text(self):
        """It holds no compiled pattern at all, so it costs nothing per message."""
        rule = compile_rule("from:@vasya", timeout=T)
        assert rule.compiled is None
        assert rule.cost_bytes == 0
        assert rule.search_all("vasya wrote this", 0.2) == []

    @pytest.mark.parametrize(
        ("raw", "sender", "expected"),
        [
            ("from:@vasya", (1, "vasya", "Вася"), True),
            ("from:@vasya", (1, "VASYA", "Вася"), True),  # case-insensitive
            ("from:@vasya", (1, "vasya2", "Вася"), False),  # exact, not a prefix
            ("from:@vasya", (1, None, "vasya"), False),  # a username is not a name
            ("from:123", (123, None, ""), True),
            ("from:123", (1234, None, ""), False),  # exact, not a prefix
            ("from:123", (None, None, ""), False),  # unknown sender matches nothing
            ("from:Вася", (1, None, "Вася Пупкин"), True),  # a name is a fragment
            ("from:Вася", (1, None, "вася 🚀"), True),  # people decorate their names
            ("from:Вася Пупкин", (1, None, "Вася"), False),
            ("from:Пупкин", (1, None, "Вася\u200b Пупкин"), True),  # zero-width junk removed
        ],
    )
    def test_who_it_recognises(self, raw, sender, expected):
        target = compile_rule(raw, timeout=T).target
        assert target is not None
        assert target.matches(*sender) is expected

    def test_at_prefix_still_means_a_mention_in_the_text(self):
        """Changing that would silently repurpose every rule anybody already wrote."""
        rule = compile_rule("@vasya", timeout=T)
        assert rule.mode is MatchMode.SUBSTRING
        assert rule.search("посмотрите на @vasya", T) is not None

    def test_the_same_username_twice_is_one_rule(self):
        assert pattern_key("from:@Vasya") == pattern_key("from:@vasya")

    def test_a_username_and_a_name_are_not_the_same_rule(self):
        """`from:@vasya` watches a username; `from:vasya` watches a name fragment. Folding them
        together refused the second as a duplicate of the first."""
        assert pattern_key("from:@vasya") != pattern_key("from:vasya")


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
