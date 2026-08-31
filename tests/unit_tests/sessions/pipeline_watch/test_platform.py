"""The package's only git-platform I/O, and its three deliberately different error policies."""

from types import SimpleNamespace

import pytest
from sessions.pipeline_watch.judgment import PipelineReport
from sessions.pipeline_watch.platform import WatchPlatform

from ..conftest import make_pipeline


class FakeClient:
    def __init__(self, *, head_sha="abc123"):
        self.head_sha = head_sha
        self.calls = []

    def get_pipeline(self, repo_id, pipeline_id):
        self.calls.append(("get_pipeline", repo_id, pipeline_id))
        return make_pipeline(pipeline_id=pipeline_id)

    def get_latest_pipeline_for_ref(self, repo_id, ref):
        self.calls.append(("get_latest_pipeline_for_ref", repo_id, ref))
        return make_pipeline(pipeline_id=999)

    def get_merge_request(self, repo_id, merge_request_id):
        self.calls.append(("get_merge_request", repo_id, merge_request_id))
        return SimpleNamespace(sha=self.head_sha)

    def create_merge_request_comment(self, repo_id, merge_request_id, body):
        self.calls.append(("create_merge_request_comment", repo_id, merge_request_id, body))


async def test_an_event_reads_the_pipeline_it_names():
    client = FakeClient()
    pipeline = await WatchPlatform("group/repo", client).aread_pipeline(ref="daiv/branch", pipeline_id=42)

    assert client.calls == [("get_pipeline", "group/repo", 42)]
    assert pipeline.id == 42


async def test_a_poll_reads_the_latest_pipeline_for_the_ref():
    client = FakeClient()
    pipeline = await WatchPlatform("group/repo", client).aread_pipeline(ref="daiv/branch", pipeline_id=None)

    assert client.calls == [("get_latest_pipeline_for_ref", "group/repo", "daiv/branch")]
    assert pipeline.id == 999


async def test_a_read_failure_propagates():
    """An unreadable pipeline is not a verdict — only the caller knows whether to stay armed."""

    class BrokenClient(FakeClient):
        def get_pipeline(self, repo_id, pipeline_id):
            raise RuntimeError("platform down")

    with pytest.raises(RuntimeError):
        await WatchPlatform("group/repo", BrokenClient()).aread_pipeline(ref="daiv/branch", pipeline_id=42)


async def test_a_pipeline_at_the_head_correlates():
    report = PipelineReport(make_pipeline("failed"))
    assert await WatchPlatform("group/repo", FakeClient()).ais_head_pipeline(merge_request_iid=7, report=report) is True


async def test_a_pipeline_from_an_earlier_push_does_not_correlate():
    report = PipelineReport(make_pipeline("failed"))
    platform = WatchPlatform("group/repo", FakeClient(head_sha="deadbee"))
    assert await platform.ais_head_pipeline(merge_request_iid=7, report=report) is False


async def test_without_a_merge_request_nothing_can_correlate():
    report = PipelineReport(make_pipeline("failed"))
    client = FakeClient()
    assert await WatchPlatform("group/repo", client).ais_head_pipeline(merge_request_iid=None, report=report) is False
    assert client.calls == []


async def test_a_transient_head_sha_failure_warns_rather_than_erroring(caplog):
    """Correlation runs on the webhook path too, so a platform outage reaches this read on every
    event — an unconditional ``logger.exception`` there is a Sentry error per event."""
    from gitlab.exceptions import GitlabGetError

    class BrokenClient(FakeClient):
        def get_merge_request(self, repo_id, merge_request_id):
            raise GitlabGetError(response_code=503)

    report = PipelineReport(make_pipeline("failed"))
    with caplog.at_level("WARNING", logger="daiv.sessions"):
        result = await WatchPlatform("group/repo", BrokenClient()).ais_head_pipeline(merge_request_iid=7, report=report)

    assert result is False
    records = [r for r in caplog.records if r.name == "daiv.sessions"]
    assert [r.levelname for r in records] == ["WARNING"]
    assert records[0].exc_info is None


async def test_an_unexpected_head_sha_failure_is_reported_with_a_traceback(caplog):
    class BrokenClient(FakeClient):
        def get_merge_request(self, repo_id, merge_request_id):
            raise AttributeError("'NoneType' object has no attribute 'sha'")

    report = PipelineReport(make_pipeline("failed"))
    with caplog.at_level("WARNING", logger="daiv.sessions"):
        result = await WatchPlatform("group/repo", BrokenClient()).ais_head_pipeline(merge_request_iid=7, report=report)

    assert result is False
    records = [r for r in caplog.records if r.name == "daiv.sessions"]
    assert [r.levelname for r in records] == ["ERROR"]
    assert records[0].exc_info is not None


async def test_a_merge_request_that_reads_back_as_none_is_reported(caplog):
    class NoneClient(FakeClient):
        def get_merge_request(self, repo_id, merge_request_id):
            return None

    report = PipelineReport(make_pipeline("failed"))
    with caplog.at_level("ERROR", logger="daiv.sessions"):
        result = await WatchPlatform("group/repo", NoneClient()).ais_head_pipeline(merge_request_iid=7, report=report)

    assert result is False
    assert any(r.levelname == "ERROR" for r in caplog.records if r.name == "daiv.sessions")


async def test_a_note_without_a_merge_request_is_a_no_op():
    client = FakeClient()
    await WatchPlatform("group/repo", client).apost_note(merge_request_iid=None, body="hi")
    assert client.calls == []


async def test_a_failing_note_never_propagates(caplog):
    """The note is best-effort: the state machine has already recorded the transition."""

    class BrokenClient(FakeClient):
        def create_merge_request_comment(self, repo_id, merge_request_id, body):
            raise RuntimeError("platform down")

    await WatchPlatform("group/repo", BrokenClient()).apost_note(merge_request_iid=7, body="hi")

    assert "failed to comment" in caplog.text


async def test_one_injected_client_serves_a_read_a_correlation_and_a_note():
    client = FakeClient()
    platform = WatchPlatform("group/repo", client)

    pipeline = await platform.aread_pipeline(ref="daiv/branch", pipeline_id=42)
    await platform.ais_head_pipeline(merge_request_iid=7, report=PipelineReport(pipeline))
    await platform.apost_note(merge_request_iid=7, body="hi")

    assert [call[0] for call in client.calls] == ["get_pipeline", "get_merge_request", "create_merge_request_comment"]


async def test_a_client_is_resolved_once_per_instance(monkeypatch):
    """Nothing injects a client in production, so every read resolves it lazily — and each
    resolution is a ``sync_to_async`` hop, because the first build in a process calls the platform.
    ``RepoClient.create_instance`` is itself cached, so what the memo saves is the hop.
    """
    built = []

    def build(**_kwargs):
        built.append(FakeClient())
        return built[-1]

    monkeypatch.setattr("sessions.pipeline_watch.platform.RepoClient.create_instance", build)
    platform = WatchPlatform("group/repo")

    await platform.apost_note(merge_request_iid=7, body="one")
    await platform.apost_note(merge_request_iid=7, body="two")

    assert len(built) == 1


async def test_a_note_resolves_its_own_client(monkeypatch):
    """``apost_note`` is reached without ``aensure_client`` from ``aexhaust`` and from the expiry
    sweep, so it cannot rely on a caller having pre-built the client: resolving it inline on the
    event loop would run the installation lookup there.
    """
    monkeypatch.setattr("sessions.pipeline_watch.platform.RepoClient.create_instance", lambda **_kw: FakeClient())
    platform = WatchPlatform("group/repo")

    await platform.apost_note(merge_request_iid=7, body="hi")

    assert (await platform._aclient()).calls == [("create_merge_request_comment", "group/repo", 7, "hi")]
