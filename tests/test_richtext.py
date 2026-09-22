from telethon.tl import types as tl

from ddiver.richtext import RichText, Span, truncate_utf16, utf16_len


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
