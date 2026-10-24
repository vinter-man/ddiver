"""A payload from a source other than Telegram renders with its own links and never a t.me one.

The Telegram links are derived from ids, and a Facebook group id pushed through the same
functions builds a plausible ``t.me/c/…`` link to a chat that is not the group. These tests pin
down that a non-Telegram payload gets only the links it carries, that Telegram payloads keep
rendering exactly as before, and that the newer fields survive the outbox round trip.
"""

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from telethon.tl import types as tl

from ddiver_core.formatter import (
    SOURCE_TELEGRAM,
    DigestEntry,
    Payload,
    Rendered,
    render_digest,
    render_notification,
)
from ddiver_core.i18n import LANGS, t
from ddiver_core.richtext import MAX_MESSAGE_UTF16, utf16_len
from ddiver_core_testing import T0

NOW = T0 + dt.timedelta(minutes=5)
GROUP = 1234567890
POST = 9876543210987654
AUTHOR = 100000000000001
POST_URL = f"https://www.facebook.com/groups/{GROUP}/posts/{POST}/"
GROUP_URL = f"https://www.facebook.com/groups/{GROUP}/"
AUTHOR_URL = "https://www.facebook.com/profile.php?id=100000000000001"

# What a Telegram row has always carried. Rows written by ddiver must stay exactly this.
TELEGRAM_KEYS = {
    "chat_id",
    "chat_title",
    "chat_username",
    "chat_kind",
    "msg_id",
    "topic_id",
    "date",
    "text",
    "spans",
    "rule_pattern",
    "rule_kind",
    "rule_is_global",
    "sender_id",
    "sender_name",
    "sender_username",
    "sender_is_channel",
    "sender_is_deleted",
    "is_forwarded",
    "catchup",
    "catchup_capped",
}


def tg_payload(**overrides: object) -> Payload:
    base: dict[str, object] = {
        "chat_id": -1001234567890,
        "chat_title": "Chat",
        "chat_username": None,
        "chat_kind": "supergroup",
        "msg_id": 77,
        "topic_id": None,
        "date": T0,
        "text": "Come play at the casino",
        "spans": [(9, 15)],
        "rule_pattern": "casino",
        "rule_kind": "include",
        "rule_is_global": False,
        "sender_id": 42,
        "sender_name": "Alex",
        "sender_username": "taras",
    }
    return Payload(**{**base, **overrides})


def fb_payload(**overrides: object) -> Payload:
    base: dict[str, object] = {
        "chat_id": GROUP,
        "chat_title": "Varna Rentals",
        "chat_username": None,
        "chat_kind": "group",
        "msg_id": POST,
        "topic_id": None,
        "date": T0,
        "text": "Renting out an apartment by the sea",
        "spans": [(15, 24)],
        "rule_pattern": "apartm",
        "rule_kind": "include",
        "rule_is_global": True,
        "sender_id": AUTHOR,
        "sender_name": "Olena K.",
        "sender_username": None,
        "source": "fb",
        "url": POST_URL,
        "chat_url": GROUP_URL,
        "sender_url": AUTHOR_URL,
    }
    return Payload(**{**base, **overrides})


def urls(rendered: Rendered) -> list[str]:
    return [e.url for e in rendered.entities if isinstance(e, tl.MessageEntityTextUrl)]


def linked(rendered: Rendered) -> dict[str, str]:
    """Visible text of every link → where it points."""
    raw = rendered.text.encode("utf-16-le")
    return {
        raw[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le"): e.url
        for e in rendered.entities
        if isinstance(e, tl.MessageEntityTextUrl)
    }


def no_telegram_links(rendered: Rendered) -> bool:
    return "t.me" not in rendered.text and all("t.me" not in url for url in urls(rendered))


class TestOutboxRoundTrip:
    def test_a_telegram_row_carries_exactly_the_keys_it_always_did(self):
        """Byte-identical rows for ddiver, and readable by a build from before the fields."""
        assert set(tg_payload().to_json()) == TELEGRAM_KEYS

    def test_a_row_from_before_the_fields_reads_as_telegram(self):
        data = tg_payload().to_json()
        restored = Payload.from_json(data)
        assert restored.source == SOURCE_TELEGRAM
        assert restored.is_telegram
        assert (restored.url, restored.chat_url, restored.sender_url) == (None, None, None)
        assert (restored.chat_private, restored.late) == (False, False)
        assert restored == tg_payload()

    def test_every_new_field_survives_the_round_trip(self):
        original = fb_payload(chat_private=True, late=True)
        data = original.to_json()
        assert data["source"] == "fb"
        assert data["url"] == POST_URL
        assert data["chat_url"] == GROUP_URL
        assert data["sender_url"] == AUTHOR_URL
        assert data["chat_private"] is True
        assert data["late"] is True
        assert Payload.from_json(data) == original

    @pytest.mark.parametrize("stored", [None, ""])
    def test_an_empty_source_means_telegram(self, stored):
        data = {**tg_payload().to_json(), "source": stored}
        assert Payload.from_json(data).source == SOURCE_TELEGRAM

    def test_default_valued_new_fields_are_not_written(self):
        """A Telegram payload that sets a link still writes only what it says."""
        data = tg_payload(url="https://example.org/m/1").to_json()
        assert set(data) == TELEGRAM_KEYS | {"url"}


class TestFacebookNotification:
    def test_every_link_is_the_one_the_payload_carries(self):
        r = render_notification(fb_payload(), lang="uk", tz="Europe/Kyiv", now=NOW)
        links = linked(r)
        assert links["Varna Rentals"] == GROUP_URL
        assert links["Olena K."] == AUTHOR_URL
        assert links[POST_URL] == POST_URL  # the link line shows the post address itself
        assert no_telegram_links(r)
        assert str(AUTHOR) in r.text  # the id stays copyable: ignoring works on it

    def test_without_links_nothing_is_linked_and_nothing_is_derived(self):
        r = render_notification(
            fb_payload(url=None, chat_url=None, sender_url=None),
            lang="en",
            tz="UTC",
            now=NOW,
        )
        assert urls(r) == []
        assert t("en", "notif.link") not in r.text  # no link line without a link
        assert "Varna Rentals" in r.text

    def test_a_group_link_alone_still_links_the_title(self):
        r = render_notification(fb_payload(url=None, sender_url=None), lang="en", tz="UTC", now=NOW)
        assert linked(r) == {"Varna Rentals": GROUP_URL}

    def test_the_post_link_stands_in_for_a_missing_group_and_profile_link(self):
        r = render_notification(
            fb_payload(chat_url=None, sender_url=None), lang="en", tz="UTC", now=NOW
        )
        links = linked(r)
        assert links["Varna Rentals"] == POST_URL
        assert links["Olena K."] == POST_URL

    def test_a_username_never_becomes_a_telegram_profile_link(self):
        r = render_notification(fb_payload(sender_username="olena"), lang="en", tz="UTC", now=NOW)
        assert "@olena" not in r.text
        assert no_telegram_links(r)

    @pytest.mark.parametrize("lang", LANGS)
    def test_a_private_group_says_the_link_is_for_members(self, lang):
        private = render_notification(fb_payload(chat_private=True), lang=lang, tz="UTC", now=NOW)
        public = render_notification(fb_payload(), lang=lang, tz="UTC", now=NOW)
        marker = t(lang, "notif.private")
        assert marker != "notif.private"
        assert marker in private.text
        assert marker not in public.text
        # right under the chat line, before the sender
        lines = private.text.split("\n")
        assert lines[1] == marker
        assert lines[2].startswith(t(lang, "notif.sender"))

    @pytest.mark.parametrize(
        ("lang", "expected"),
        [
            ("en", "⏳ Posted 20 h ago, appeared only now"),
            ("ru", "⏳ Опубликовано 20 ч назад, появилось только сейчас"),
            ("uk", "⏳ Опубліковано 20 год тому, з'явилося лише зараз"),
        ],
    )
    def test_a_late_post_says_when_it_was_posted(self, lang, expected):
        now = T0 + dt.timedelta(hours=20)
        late = render_notification(fb_payload(late=True), lang=lang, tz="UTC", now=now)
        on_time = render_notification(fb_payload(), lang=lang, tz="UTC", now=now)
        assert expected in late.text.split("\n")
        assert expected not in on_time.text
        # the late line follows the time line and precedes the rule line
        lines = late.text.split("\n")
        at = lines.index(expected)
        assert lines[at - 1].startswith(t(lang, "notif.time"))
        assert lines[at + 1].startswith(t(lang, "notif.rule"))

    def test_the_markers_do_not_disturb_the_bold_match(self):
        r = render_notification(
            fb_payload(chat_private=True, late=True), lang="en", tz="UTC", now=NOW
        )
        bold = [e for e in r.entities if isinstance(e, tl.MessageEntityBold)]
        raw = r.text.encode("utf-16-le")
        assert [
            raw[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le") for e in bold
        ] == ["apartment"]

    @pytest.mark.parametrize(
        "bad",
        [
            "javascript:alert(1)",
            "tg://user?id=1",
            "/groups/1/posts/2/",
            "",
            "https://[::1",  # urlsplit raises on it
            "https:///no-host",
            # urlsplit parses these by removing the break, but the original would be shown
            f"{POST_URL}\n🔒 Rule: fake",
            f"{POST_URL}\r\nx",
            f"{POST_URL}\tx",
            f" {POST_URL}",
            f"{POST_URL}x y",
            f"{POST_URL}\u00a0",  # no-break space
            f"{POST_URL}\u2028x",  # line separator
            f"{POST_URL}\x00",
            f"{POST_URL}\x85",  # C1 next-line
            "https://www.facebook.com/\u202eputs/12/",  # bidi override: reads as another path
            f"{POST_URL}\u200d",  # zero-width joiner
            "https://www.facebook.com/" + "a" * 2024,  # one over the limit
        ],
    )
    def test_a_link_that_is_not_a_web_address_is_dropped(self, bad):
        r = render_notification(
            fb_payload(url=bad, chat_url=bad, sender_url=bad), lang="en", tz="UTC", now=NOW
        )
        assert urls(r) == []
        assert not bad or bad not in r.text  # nor shown on the link line

    @pytest.mark.parametrize(
        "good",
        [
            "https://www.facebook.com/" + "a" * 2023,  # exactly the limit
            "https://www.facebook.com/people/Olena-K/100000000000001/",
            "HTTPS://WWW.FACEBOOK.COM/groups/1/",
            "http://example.org/a?b=c&d=%20e#f",
        ],
    )
    def test_a_plain_web_address_is_kept_as_it_is(self, good):
        r = render_notification(fb_payload(url=good), lang="en", tz="UTC", now=NOW)
        assert good in urls(r)

    def test_a_damaged_row_with_a_non_text_link_renders_without_it(self):
        data = {**fb_payload().to_json(), "url": 12345, "chat_url": ["x"], "sender_url": {}}
        r = render_notification(Payload.from_json(data), lang="en", tz="UTC", now=NOW)
        assert urls(r) == []

    def test_an_unknown_source_is_treated_like_facebook_not_like_telegram(self):
        r = render_notification(
            fb_payload(source="imap", url=None, chat_url=None, sender_url=None),
            lang="en",
            tz="UTC",
            now=NOW,
        )
        assert urls(r) == []


class TestTelegramPayloadWithLinks:
    def test_a_carried_message_link_wins_over_the_derived_one(self):
        own = "https://example.org/archive/77"
        r = render_notification(tg_payload(url=own), lang="en", tz="UTC", now=NOW)
        assert "https://t.me/c/1234567890/77" not in urls(r)
        assert urls(r).count(own) == 3  # chat title (no username), sender name, link line
        assert "https://t.me/taras" in urls(r)  # the username is still a Telegram profile

    def test_a_carried_chat_link_wins_over_the_derived_one(self):
        own = "https://example.org/chat"
        r = render_notification(
            tg_payload(chat_username="pub", chat_url=own), lang="en", tz="UTC", now=NOW
        )
        assert linked(r)["Chat"] == own
        assert "https://t.me/pub/77" in urls(r)

    def test_a_carried_profile_link_is_used_for_the_name(self):
        r = render_notification(
            tg_payload(sender_url="https://example.org/u/42"), lang="en", tz="UTC", now=NOW
        )
        assert linked(r)["Alex"] == "https://example.org/u/42"


def digest_of(*payloads: Payload) -> list[DigestEntry]:
    return [DigestEntry(p, (1, p.chat_id, p.msg_id + i)) for i, p in enumerate(payloads)]


class TestFacebookDigest:
    def test_the_group_and_every_item_link_where_the_payloads_say(self):
        second = fb_payload(msg_id=POST + 1, url=f"{GROUP_URL}posts/{POST + 1}/")
        r = render_digest(digest_of(fb_payload(), second), lang="en", tz="UTC", now=NOW)
        assert urls(r) == [GROUP_URL, POST_URL, f"{GROUP_URL}posts/{POST + 1}/"]
        assert no_telegram_links(r)

    def test_a_private_group_is_marked_in_its_heading(self):
        r = render_digest(digest_of(fb_payload(chat_private=True)), lang="en", tz="UTC", now=NOW)
        heading = r.text.split("\n")[2]
        assert heading == "📡 Varna Rentals 🔒 — 1"
        public = render_digest(digest_of(fb_payload()), lang="en", tz="UTC", now=NOW)
        assert "🔒" not in public.text

    def test_without_a_group_link_the_heading_points_at_the_first_post(self):
        r = render_digest(digest_of(fb_payload(chat_url=None)), lang="en", tz="UTC", now=NOW)
        assert urls(r) == [POST_URL, POST_URL]

    @pytest.mark.parametrize("lang", LANGS)
    def test_private_groups_with_their_own_links_still_fit_one_message(self, lang):
        """ddiver's per-line budget, with what a Facebook digest adds: the 🔒 after every
        heading and a carried link on every line.

        The chat count is swept, as in ddiver's own test: the overflow it guards against
        lived at one specific shape.
        """
        worst = 0
        for chats in range(1, 17):
            entries = []
            for i in range(200):
                gid = GROUP + i % chats
                entries.append(
                    DigestEntry(
                        fb_payload(
                            chat_id=gid,
                            chat_title="C" * 128,
                            chat_private=True,
                            chat_url=f"https://www.facebook.com/groups/{gid}/",
                            msg_id=POST + i,
                            url=f"https://www.facebook.com/groups/{gid}/posts/{POST + i}/",
                            rule_pattern="re:(" + "|".join(f"word{j}" for j in range(30)) + ")",
                            sender_name="I" * 129,
                            text="casino " * 40,
                        ),
                        (1, gid, POST + i),
                    )
                )
            r = render_digest(entries, lang=lang, tz="UTC", now=NOW, overflow=999_999)
            worst = max(worst, utf16_len(r.text))
            assert all(e.offset + e.length <= utf16_len(r.text) for e in r.entities)
        assert worst <= MAX_MESSAGE_UTF16

    def test_without_any_link_nothing_is_linked(self):
        r = render_digest(
            digest_of(fb_payload(url=None, chat_url=None), fb_payload(url=None, chat_url=None)),
            lang="uk",
            tz="UTC",
            now=NOW,
        )
        assert urls(r) == []
        assert "Varna Rentals" in r.text


ids = st.integers(min_value=-(2**63), max_value=2**63 - 1)
usernames = st.one_of(st.none(), st.from_regex(r"[a-z][a-z0-9_]{3,15}", fullmatch=True))


@settings(max_examples=200, deadline=None)
@given(
    chat_id=ids,
    msg_id=st.integers(min_value=1, max_value=2**63 - 1),
    topic_id=st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
    chat_username=usernames,
    sender_username=usernames,
    chat_kind=st.sampled_from(["group", "supergroup", "channel"]),
    source=st.sampled_from(["fb", "imap", "webhook"]),
    lang=st.sampled_from(LANGS),
)
def test_no_source_but_telegram_ever_gets_a_telegram_link(
    *, chat_id, msg_id, topic_id, chat_username, sender_username, chat_kind, source, lang
):
    payload = fb_payload(
        chat_id=chat_id,
        msg_id=msg_id,
        topic_id=topic_id,
        chat_username=chat_username,
        sender_username=sender_username,
        chat_kind=chat_kind,
        source=source,
        url=None,
        chat_url=None,
        sender_url=None,
    )
    assert no_telegram_links(render_notification(payload, lang=lang, tz="UTC", now=NOW))
    assert no_telegram_links(render_digest(digest_of(payload), lang=lang, tz="UTC", now=NOW))
