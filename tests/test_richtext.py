from telethon.tl import types as tl

from ddiver_core.richtext import MAX_ENTITIES, RichText, Span, truncate_utf16, utf16_len, window


def test_utf16_len_counts_astral_chars_twice():
    assert utf16_len("abc") == 3
    assert utf16_len("👍") == 2
    assert utf16_len("Привет 👍🏽") == 7 + 2 + 2


def test_truncate_does_not_split_code_points():
    text = "ab👍cd"
    assert truncate_utf16(text, 2) == "ab"
    assert truncate_utf16(text, 3) == "ab"  # emoji needs 2 units, would exceed
    assert truncate_utf16(text, 4) == "ab👍"
    assert truncate_utf16(text, 100) == text
    assert truncate_utf16(text, 0) == ""


def test_entities_have_utf16_offsets():
    rt = RichText()
    rt.add("👍 ").add("Chat <&>", url="https://t.me/c/1/2").add(" ").add("id 5", code=True)
    ents = rt.entities()
    assert rt.text() == "👍 Chat <&> id 5"
    url = next(e for e in ents if isinstance(e, tl.MessageEntityTextUrl))
    assert (url.offset, url.length, url.url) == (3, 8, "https://t.me/c/1/2")
    code = next(e for e in ents if isinstance(e, tl.MessageEntityCode))
    assert (code.offset, code.length) == (12, 4)
    assert rt.utf16_length == 16


def test_bold_and_url_can_stack():
    rt = RichText().add("x", bold=True, url="tg://user?id=1")
    kinds = {type(e) for e in rt.entities()}
    assert kinds == {tl.MessageEntityBold, tl.MessageEntityTextUrl}


def test_empty_add_is_noop():
    rt = RichText().add("", bold=True)
    assert rt.text() == "" and rt.entities() == []


def test_bold_spans_merge_and_clip():
    text = "aaa bbb ccc"
    spans = [Span(4, 7), Span(0, 2), Span(1, 5), Span(9, 50), Span(7, 7)]
    rt = RichText().add_with_bold_spans(text, spans)
    assert rt.text() == text
    bolds = sorted(
        (e.offset, e.length) for e in rt.entities() if isinstance(e, tl.MessageEntityBold)
    )
    assert bolds == [(0, 7), (9, 2)]


def test_bold_span_after_emoji_uses_utf16_offset():
    text = "👍 spam"
    rt = RichText().add_with_bold_spans(text, [Span(2, 6)])
    bold = next(e for e in rt.entities() if isinstance(e, tl.MessageEntityBold))
    assert (bold.offset, bold.length) == (3, 4)


def test_build_strips_trailing_whitespace_and_clips_entities():
    rt = RichText().add("hi ", url="https://x").add("there  \n ", bold=True)
    text, ents = rt.build()
    assert text == "hi there"
    kinds = {(type(e).__name__, e.offset, e.length) for e in ents}
    assert kinds == {("MessageEntityTextUrl", 0, 3), ("MessageEntityBold", 3, 5)}
    # entity fully inside the stripped tail disappears
    rt2 = RichText().add("x").add("   ", code=True)
    text2, ents2 = rt2.build()
    assert text2 == "x" and ents2 == []


def test_build_caps_entities():
    """Telegram rejects a message with more than 100 entities, so the cap must stay below it."""
    assert MAX_ENTITIES < 100
    rt = RichText()
    for i in range(MAX_ENTITIES + 20):
        rt.add(f"{i} ", bold=True)
    _, ents = rt.build()
    assert len(ents) == MAX_ENTITIES


def test_window_keeps_whole_text_when_it_fits():
    w = window("short text", [Span(0, 5)], 100)
    assert (w.text, w.spans, w.cut_before, w.cut_after) == ("short text", [Span(0, 5)], 0, 0)


def test_window_uses_head_when_match_is_early():
    text = "spam " * 100  # 500 chars
    w = window(text, [Span(0, 4)], 60)
    assert w.text == text[:60]
    assert w.spans == [Span(0, 4)]
    assert (w.cut_before, w.cut_after) == (0, 440)


def test_window_recentres_a_match_sitting_near_the_end_of_the_head():
    """The head branch must leave room after the match, not merely contain it.

    A match that lands inside the head but within half a context of its end would be shown
    with no text following it — which is exactly the part a moderator needs to judge a
    message. Relaxing the boundary to `first_start < len(head)` survives every other test.
    """
    text = "a" * 250 + "casino" + "b" * 250
    limit, context = 300, 160
    w = window(text, [Span(250, 256)], limit, context=context)

    assert w.cut_before > 0, "the match sat too close to the end of the head to use it"
    shown_after = len(w.text) - (w.spans[0].end)
    assert shown_after >= context // 2  # there is context on the far side of the match
    assert w.text[w.spans[0].start : w.spans[0].end] == "casino"


def test_window_centres_on_late_match_and_rebases_spans():
    text = "a" * 1000 + "casino" + "b" * 1000
    w = window(text, [Span(1000, 1006)], 300, context=100)
    assert w.cut_before == 900
    assert w.text.startswith("a" * 100 + "casino")
    assert w.spans == [Span(100, 106)]
    assert w.text[w.spans[0].start : w.spans[0].end] == "casino"
    assert utf16_len(w.text) <= 300
    assert w.cut_after == len(text) - 900 - len(w.text)


def test_window_with_emoji_respects_utf16_limit():
    text = "👍" * 3000
    w = window(text, [], 4000)
    assert utf16_len(w.text) <= 4000
    assert len(w.text) == 2000
