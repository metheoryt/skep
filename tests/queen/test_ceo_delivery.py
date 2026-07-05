from skep.queen.app import make_ceo_callbacks


class _Gateway:
    def __init__(self):
        self.posts = []

    async def post(self, topic_id, text):
        self.posts.append((topic_id, text))
        return len(self.posts)


async def test_deliver_ceo_posts_formatted_message():
    from skep.queen.mailbox import Message, STATUS_UNREAD
    gw = _Gateway()
    deliver, alert = make_ceo_callbacks(gw, topic_id=99)
    msg = Message(id=5, sender="mgr:alice", recipient="ceo", subject="status",
                  body="all green", created_at=1.0, in_reply_to=None, hops=0,
                  status=STATUS_UNREAD, dead_letter_reason=None)
    await deliver(msg)
    assert len(gw.posts) == 1
    topic, text = gw.posts[0]
    assert topic == 99
    assert "mgr:alice" in text
    assert "status" in text
    assert "all green" in text
    assert "5" in text


async def test_alert_ceo_posts_warning():
    gw = _Gateway()
    _, alert = make_ceo_callbacks(gw, topic_id=99)
    await alert("loop stopped")
    assert gw.posts[0][1].endswith("loop stopped") or "loop stopped" in gw.posts[0][1]


async def test_deliver_ceo_escapes_markdownv2_reserved_chars():
    from skep.queen.mailbox import Message, STATUS_UNREAD
    gw = _Gateway()
    deliver, _ = make_ceo_callbacks(gw, topic_id=None)
    msg = Message(id=7, sender="mgr:alice", recipient="ceo",
                  subject="build passed.", body="all-green (v2)!",
                  created_at=1.0, in_reply_to=None, hops=0,
                  status=STATUS_UNREAD, dead_letter_reason=None)
    await deliver(msg)
    text = gw.posts[0][1]
    # dynamic reserved chars must be backslash-escaped for MarkdownV2
    assert "\\." in text      # the period in "passed."
    assert "\\(" in text and "\\)" in text   # the parens in "(v2)"
    assert "\\-" in text      # the hyphen in "all-green"
    assert "\\!" in text      # the bang
    # no HTML tags leaked
    assert "<b>" not in text and "<i>" not in text


async def test_deliver_ceo_maps_telegram_badrequest_to_permanent():
    """A Telegram 400 (e.g. body over the 4096-char limit) must surface as
    PermanentDeliveryError so redeliver_ceo dead-letters it instead of
    retrying forever."""
    import pytest
    from aiogram.exceptions import TelegramBadRequest

    from skep.queen.mailbox import (
        Message, STATUS_UNREAD, PermanentDeliveryError,
    )

    class _BadGateway:
        async def post(self, topic_id, text):
            raise TelegramBadRequest(method=None, message="message is too long")

    deliver, _ = make_ceo_callbacks(_BadGateway(), topic_id=None)
    msg = Message(id=1, sender="mgr:a", recipient="ceo", subject="s",
                  body="x" * 5000, created_at=1.0, in_reply_to=None, hops=0,
                  status=STATUS_UNREAD, dead_letter_reason=None)
    with pytest.raises(PermanentDeliveryError):
        await deliver(msg)


async def test_alert_ceo_escapes_markdownv2_reserved_chars():
    gw = _Gateway()
    _, alert = make_ceo_callbacks(gw, topic_id=None)
    await alert("2 message(s) undeliverable: agent 3 finished.")
    text = gw.posts[0][1]
    assert "\\(" in text and "\\)" in text and "\\." in text
