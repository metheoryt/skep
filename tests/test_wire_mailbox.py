# tests/test_wire_mailbox.py
from skep import wire


def test_mailbox_send_roundtrip():
    msg = wire.mailbox_send_msg(
        req_id="r1", tid=7, to="ceo", subject="s", body="b",
        in_reply_to=None)
    assert msg["t"] == wire.MAILBOX_SEND
    back = wire.decode(wire.encode(msg))
    assert back == msg
    assert back["req_id"] == "r1"
    assert back["tid"] == 7
    assert back["to"] == "ceo"
    assert back["in_reply_to"] is None


def test_mailbox_ack_roundtrip():
    msg = wire.mailbox_ack_msg(
        req_id="r1", ok=True, message_id=42, error=None, status="delivered")
    assert msg["t"] == wire.MAILBOX_ACK
    back = wire.decode(wire.encode(msg))
    assert back == msg
    assert back["message_id"] == 42


def test_inbox_read_roundtrip():
    msg = wire.inbox_read_msg(req_id="r2", tid=7)
    assert msg["t"] == wire.INBOX_READ
    assert wire.decode(wire.encode(msg)) == msg


def test_inbox_reply_roundtrip():
    payload = [{"id": 1, "sender": "ceo", "subject": "s", "body": "b"}]
    msg = wire.inbox_reply_msg(req_id="r2", messages=payload)
    assert msg["t"] == wire.INBOX_REPLY
    back = wire.decode(wire.encode(msg))
    assert back == msg
    assert back["messages"] == payload
