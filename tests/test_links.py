from telethon.tl import types as tl

from ddiver.links import ChatRef, chat_link, message_link, topic_id_of, user_link

PUBLIC = ChatRef(-1001234567890, "Pub", "pubchat", "supergroup")
PRIVATE = ChatRef(-1001234567890, "Priv", None, "supergroup")
FORUM = ChatRef(-1009876543210, "Forum", None, "supergroup", is_forum=True)
BASIC = ChatRef(-123456, "Old group", None, "group")
CHANNEL = ChatRef(-1005555555555, "News", "newschan", "channel")


def test_chat_links():
    assert chat_link(PUBLIC) == "https://t.me/pubchat"
    assert chat_link(CHANNEL) == "https://t.me/newschan"
    assert chat_link(PRIVATE) is None
    assert chat_link(BASIC) is None


def test_message_links():
    assert message_link(PUBLIC, 42) == "https://t.me/pubchat/42"
    assert message_link(PRIVATE, 42) == "https://t.me/c/1234567890/42"
    assert message_link(FORUM, 42, topic_id=7) == "https://t.me/c/9876543210/7/42"
    assert message_link(PUBLIC, 42, topic_id=7) == "https://t.me/pubchat/7/42"
    assert message_link(BASIC, 42) is None


def test_user_links():
    assert user_link(5, "alice") == "https://t.me/alice"
    assert user_link(5, None) == "tg://user?id=5"


def _msg(reply_to=None):
    return tl.Message(id=1, peer_id=tl.PeerChannel(1), message="x", reply_to=reply_to, date=None)


def test_topic_id_of():
    assert topic_id_of(_msg()) is None
    plain_reply = tl.MessageReplyHeader(reply_to_msg_id=10)
    assert topic_id_of(_msg(plain_reply)) is None
    in_topic = tl.MessageReplyHeader(reply_to_msg_id=77, forum_topic=True)
    assert topic_id_of(_msg(in_topic)) == 77
    reply_in_topic = tl.MessageReplyHeader(reply_to_msg_id=80, reply_to_top_id=77, forum_topic=True)
    assert topic_id_of(_msg(reply_in_topic)) == 77


def test_chat_ref_from_entities():
    ch = tl.Channel(
        id=1234567890,
        title="T",
        photo=tl.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
        username="tt",
        forum=True,
    )
    ref = ChatRef.from_entity(ch)
    assert (ref.id, ref.bare_id, ref.kind, ref.is_forum, ref.username) == (
        -1001234567890,
        1234567890,
        "supergroup",
        True,
        "tt",
    )
    broadcast = tl.Channel(id=5, title="N", photo=tl.ChatPhotoEmpty(), date=None, broadcast=True)
    assert ChatRef.from_entity(broadcast).kind == "channel"
    basic = tl.Chat(
        id=9, title="B", photo=tl.ChatPhotoEmpty(), participants_count=3, date=None, version=1
    )
    b = ChatRef.from_entity(basic)
    assert (b.id, b.kind, b.is_basic_group) == (-9, "group", True)
    forbidden = tl.ChannelForbidden(id=3, access_hash=0, title="Gone", megagroup=True)
    assert ChatRef.from_entity(forbidden).username is None
