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
