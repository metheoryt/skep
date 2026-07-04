from fleetd.transport import InMemoryEventSink


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
