from skep.transport import InMemoryEventSink, SwitchableEventSink


class RecordingInbox:
    def __init__(self):
        self.calls = []

    async def on_task_started(self, host, profile, local_id, repo, title):
        self.calls.append(("started", host, profile, local_id, repo, title))

    async def on_activity(self, host, profile, local_id, line):
        self.calls.append(("activity", host, profile, local_id, line))

    async def on_milestone(self, host, profile, local_id, text):
        self.calls.append(("milestone", host, profile, local_id, text))

    async def on_done(self, host, profile, local_id, status, summary):
        self.calls.append(("done", host, profile, local_id, status, summary))


async def test_in_memory_sink_stamps_identity_and_forwards():
    inbox = RecordingInbox()
    sink = InMemoryEventSink(inbox, host="g16", profile="work")

    await sink.task_started(5, "nix", "clean nvidia")
    await sink.activity(5, "🔧 edit_file")
    await sink.milestone(5, "✅ Done: finished")
    await sink.done(5, "done", "finished")

    assert inbox.calls == [
        ("started", "g16", "work", 5, "nix", "clean nvidia"),
        ("activity", "g16", "work", 5, "🔧 edit_file"),
        ("milestone", "g16", "work", 5, "✅ Done: finished"),
        ("done", "g16", "work", 5, "done", "finished"),
    ]


class _Rec:
    def __init__(self):
        self.calls = []

    async def task_started(self, local_id, repo, title, session_local_id=None):
        self.calls.append(("task_started", local_id, repo, title))

    async def activity(self, local_id, line):
        self.calls.append(("activity", local_id, line))

    async def milestone(self, local_id, text):
        self.calls.append(("milestone", local_id, text))

    async def done(self, local_id, status, summary):
        self.calls.append(("done", local_id, status, summary))


async def test_switchable_forwards_to_target():
    rec = _Rec()
    s = SwitchableEventSink()
    s.target = rec
    await s.task_started(1, "nix", "t")
    assert rec.calls == [("task_started", 1, "nix", "t")]


async def test_switchable_drops_when_detached():
    s = SwitchableEventSink()
    s.target = None
    await s.activity(1, "line")  # no target -> no error, dropped
    await s.done(1, "done", "ok")


async def test_in_memory_sink_accepts_session_local_id():
    recorded = {}

    class RecordingInbox:
        async def on_task_started(self, host, profile, local_id, repo, title):
            recorded.update(local_id=local_id, repo=repo, title=title)

    sink = InMemoryEventSink(RecordingInbox(), "h1", "default")
    await sink.task_started(7, "nix", "t", 7)   # 4-arg is the new contract
    await sink.task_started(8, "nix", "t")       # 3-arg still valid (optional)
    assert recorded["local_id"] == 8
