import datetime as dt

import pytest
from telethon.tl import types as tl

from ddiver.formatter import (
    DIGEST_CHATS,
    DIGEST_ITEMS_PER_CHAT,
    DigestEntry,
    Payload,
    Rendered,
    render_digest,
    render_notification,
)
from ddiver.richtext import MAX_MESSAGE_UTF16, utf16_len
from tests.fakes import T0

NOW = T0 + dt.timedelta(seconds=12)


def payload(**overrides: object) -> Payload:
    base = {
        "chat_id": -1001234567890,
        "chat_title": "Чат <&> модерации",
        "chat_username": None,
        "chat_kind": "supergroup",
        "msg_id": 77,
        "topic_id": None,
        "date": T0,
        "text": "Заходи в казино прямо сейчас",
        "spans": [(9, 15)],
        "rule_pattern": "казино",
        "rule_kind": "include",
        "rule_is_global": False,
        "sender_id": 42,
        "sender_name": "Иван",
        "sender_username": "ivan",
    }
    return Payload(**{**base, **overrides})


def urls(rendered: Rendered) -> list[str]:
    return [e.url for e in rendered.entities if isinstance(e, tl.MessageEntityTextUrl)]


def bolds(rendered: Rendered) -> list[str]:
    return [
        _slice_utf16(rendered.text, e.offset, e.length)
        for e in rendered.entities
        if isinstance(e, tl.MessageEntityBold)
    ]


def codes(rendered: Rendered) -> list[str]:
    return [
        _slice_utf16(rendered.text, e.offset, e.length)
        for e in rendered.entities
        if isinstance(e, tl.MessageEntityCode)
    ]


def _slice_utf16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le")


class TestNotification:
    def test_private_supergroup_layout(self):
        r = render_notification(payload(), lang="ru", tz="Europe/Kyiv", now=NOW)
        link = "https://t.me/c/1234567890/77"
        assert "Чат <&> модерации" in r.text
        assert "2026-09-22 15:00:00 +03:00 (Europe/Kyiv)" in r.text
        assert "12 сек назад" in r.text
        assert "правило чата" in r.text
        assert "Заходи в казино прямо сейчас" in r.text
        assert urls(r).count(link) == 3  # chat title, sender name, message link
        assert "https://t.me/ivan" in urls(r)
        assert codes(r) == ["42", "казино"]
        assert bolds(r) == ["казино"]

    def test_public_chat_and_topic_links(self):
        r = render_notification(
            payload(chat_username="pubchat", topic_id=5), lang="en", tz="UTC", now=NOW
        )
        assert "https://t.me/pubchat" in urls(r)
        assert "https://t.me/pubchat/5/77" in urls(r)

    def test_basic_group_has_no_links(self):
        r = render_notification(
            payload(chat_id=-555, chat_kind="group"), lang="en", tz="UTC", now=NOW
        )
        assert urls(r) == ["https://t.me/ivan"]
        assert "42" in codes(r)

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"sender_is_deleted": True, "sender_name": "", "sender_username": None}, "Deleted"),
            (
                {
                    "sender_id": -1001234567890,
                    "sender_name": "",
                    "sender_username": None,
                    "sender_is_channel": True,
                },
                "Anonymous admin",
            ),
            ({"sender_id": None, "sender_name": "", "sender_username": None}, "Unknown sender"),
        ],
    )
    def test_sender_variants(self, overrides, expected):
        r = render_notification(payload(**overrides), lang="en", tz="UTC", now=NOW)
        assert expected in r.text

    def test_forwarded_and_catchup_markers(self):
        r = render_notification(
            payload(is_forwarded=True, catchup=True, catchup_skipped=120),
            lang="uk",
            tz="Europe/Kyiv",
            now=NOW,
        )
        assert "переслано" in r.text
        assert "офлайн" in r.text
        assert "120" in r.text

    def test_global_rule_scope(self):
        r = render_notification(payload(rule_is_global=True), lang="en", tz="UTC", now=NOW)
        assert "rule for all chats" in r.text

    def test_long_text_is_windowed_around_the_match(self):
        text = "a" * 5000 + " казино " + "b" * 5000
        start = text.index("казино")
        r = render_notification(
            payload(text=text, spans=[(start, start + 6)]), lang="en", tz="UTC", now=NOW
        )
        assert utf16_len(r.text) <= MAX_MESSAGE_UTF16
        assert "казино" in r.text
        assert bolds(r) == ["казино"]
        assert r.text.count("…") >= 2
        assert all(e.offset + e.length <= utf16_len(r.text) for e in r.entities)

    def test_emoji_text_stays_within_the_utf16_limit(self):
        text = "👍" * 3000 + "казино"
        r = render_notification(
            payload(text=text, spans=[(3000, 3006)]), lang="en", tz="UTC", now=NOW
        )
        assert utf16_len(r.text) <= MAX_MESSAGE_UTF16
        assert bolds(r) == ["казино"]

    def test_multiple_match_spans_are_all_bold(self):
        text = "казино и ещё казино"
        r = render_notification(
            payload(text=text, spans=[(0, 6), (13, 19)]), lang="en", tz="UTC", now=NOW
        )
        assert bolds(r) == ["казино", "казино"]

    def test_empty_text_renders_headers_only(self):
        r = render_notification(payload(text="", spans=[]), lang="en", tz="UTC", now=NOW)
        assert r.text.rstrip().endswith(")")
        assert all(e.offset + e.length <= utf16_len(r.text) for e in r.entities)

    def test_language_and_timezone_are_applied_at_render_time(self):
        p = payload()
        ru = render_notification(p, lang="ru", tz="Europe/Kyiv", now=NOW)
        uk = render_notification(p, lang="uk", tz="UTC", now=NOW + dt.timedelta(minutes=5))
        assert "12 сек назад" in ru.text
        assert "5 хв тому" in uk.text
        assert "(UTC)" in uk.text


class TestDigest:
    def entries(self, count: int, chats: int = 1) -> list[DigestEntry]:
        out = []
        for i in range(count):
            chat_id = -1001234567890 - (i % chats)
            p = payload(
                chat_id=chat_id,
                chat_title=f"Chat {i % chats}",
                msg_id=100 + i,
                text=f"сообщение {i} про казино",
                spans=[(16, 22)],
            )
            out.append(DigestEntry(p, (1, chat_id, 100 + i)))
        return out

    def test_groups_by_chat_and_caps_items(self):
        r = render_digest(self.entries(12, chats=2), lang="en", tz="UTC", now=NOW)
        assert "12 matches" in r.text
        assert r.text.count("• ") == 2 * DIGEST_ITEMS_PER_CHAT
        assert "and 1 more" in r.text  # 6 items per chat, 5 shown
        assert utf16_len(r.text) <= MAX_MESSAGE_UTF16

    def test_many_chats_are_summarised(self):
        r = render_digest(self.entries(60, chats=20), lang="ru", tz="UTC", now=NOW)
        assert "и ещё" in r.text
        assert r.text.count("📡") <= DIGEST_CHATS
        assert utf16_len(r.text) <= MAX_MESSAGE_UTF16
        assert all(e.offset + e.length <= utf16_len(r.text) for e in r.entities)

    def test_overflow_is_reported(self):
        r = render_digest(self.entries(3), lang="en", tz="UTC", now=NOW, overflow=17)
        assert "17 matches were dropped" in r.text

    def test_entity_cap_is_respected(self):
        r = render_digest(self.entries(200, chats=15), lang="en", tz="UTC", now=NOW)
        assert len(r.entities) <= 90
        assert utf16_len(r.text) <= MAX_MESSAGE_UTF16


class TestPayloadJson:
    def test_roundtrip(self):
        p = payload(topic_id=9, catchup=True, sender_is_channel=True)
        assert Payload.from_json(p.to_json()) == p

    def test_defaults_for_missing_optional_fields(self):
        minimal = {
            "chat_id": -1001,
            "chat_title": "T",
            "msg_id": 5,
            "date": T0.isoformat(),
        }
        p = Payload.from_json(minimal)
        assert (p.text, p.spans, p.sender_id, p.catchup) == ("", [], None, False)
        assert p.date == T0


class TestSenderWithoutAName:
    def test_the_id_is_shown_instead_of_unknown(self):
        """A basic group's update has no entities; the id is all we have, and it is enough."""
        r = render_notification(
            payload(sender_name="", sender_username=None, sender_id=4242),
            lang="ru",
            tz="UTC",
            now=NOW,
        )
        assert "Пользователь 4242" in r.text
        assert "Неизвестный отправитель" not in r.text
        assert "4242" in codes(r)

    def test_unknown_only_when_there_is_no_id_at_all(self):
        r = render_notification(
            payload(sender_name="", sender_username=None, sender_id=None),
            lang="ru",
            tz="UTC",
            now=NOW,
        )
        assert "Неизвестный отправитель" in r.text
