import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from gitlab.exceptions import GitlabGetError
from sessions.inbox.drain import Drain
from sessions.models import EventKind, InboxEvent, Session, SessionOrigin, SessionSubmodule
from sessions.submodule_merges import SubmoduleMerges
from sessions.tasks import settle_submodule_merge_task

pytestmark = pytest.mark.django_db(transaction=True)

MERGE_REQUEST = {
    "ref": "daiv/core",
    "merge_request_iid": 12,
    "merge_request_web_url": "https://git/group/core/-/merge_requests/12",
    "merge_request_title": "Core fix",
    "merge_request_draft": True,
}


@pytest.fixture(autouse=True)
def _no_drain():
    with patch.object(Drain, "start_logged"):
        yield


def _client(*, merged="1a2b3c4d", contains=True, error=None):
    client = Mock()
    client.get_merged_commit.side_effect = error
    client.get_merged_commit.return_value = merged
    client.is_ancestor.return_value = contains
    return client


async def _row(*, archive_pending=True, **fields) -> SessionSubmodule:
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/app",
        ref="daiv/app",
        archive_pending=archive_pending,
    )
    defaults = {
        "root_repo_id": "group/app",
        "parent_repo_id": "group/app",
        "path": "libs/core",
        "repo_id": "group/core",
        "recorded_sha": "9f8e7d6c",
        "merge_pending": True,
        **MERGE_REQUEST,
    }
    return await SessionSubmodule.objects.acreate(session=session, **(defaults | fields))


async def _settle(client) -> None:
    with patch("sessions.submodule_merges.RepoClient.create_instance", return_value=client):
        await settle_submodule_merge_task.func("group/core", 12)


async def _assert_cleared(row: SessionSubmodule) -> None:
    await row.arefresh_from_db()
    assert (row.ref, row.merge_request_iid, row.merge_request_web_url, row.merge_request_title) == ("", None, "", "")
    assert (row.merge_request_draft, row.merge_pending) == (False, False)


async def _archived(row: SessionSubmodule) -> bool:
    return (await Session.objects.aget(pk=row.session_id)).archived_at is not None


class TestSettle:
    async def test_a_merge_containing_the_recorded_commit_clears_the_row_and_archives_the_pending_session(self):
        row = await _row()
        client = _client(contains=True)

        await _settle(client)

        client.is_ancestor.assert_called_once_with("group/core", "9f8e7d6c", "1a2b3c4d")
        await _assert_cleared(row)
        assert not await InboxEvent.objects.aexists()
        assert await _archived(row)

    async def test_a_merge_without_the_recorded_commit_wakes_the_session_to_record_it(self):
        row = await _row()

        await _settle(_client(contains=False))

        event = await InboxEvent.objects.aget(session_id=row.session_id)
        assert (event.kind, event.wake, event.repo_id, event.merge_request_iid) == (
            EventKind.SUBMODULE_MERGED, True, "group/core", 12,
        )  # fmt: skip
        assert event.source_key == "submodule_merged:group/core!12"
        assert event.payload == {
            "path": "libs/core",
            "root_repo_id": "group/app",
            "merge_request_iid": 12,
            "recorded_sha": "9f8e7d6c",
            "merged_sha": "1a2b3c4d",
        }
        await _assert_cleared(row)
        assert not await _archived(row)

    async def test_a_redelivered_merge_posts_one_event(self):
        row = await _row()

        await _settle(_client(contains=False))
        await SessionSubmodule.objects.filter(pk=row.pk).aupdate(merge_pending=True, merge_request_iid=12)
        await _settle(_client(contains=False))

        assert await InboxEvent.objects.filter(session_id=row.session_id).acount() == 1

    async def test_a_parent_recording_none_of_daivs_commits_needs_nothing(self):
        row = await _row(recorded_sha="")
        client = _client(contains=False)

        await _settle(client)

        client.is_ancestor.assert_not_called()
        await _assert_cleared(row)
        assert not await InboxEvent.objects.aexists()

    @pytest.mark.parametrize(
        "client",
        [_client(error=GitlabGetError("boom", 500)), _client(merged=None)],
        ids=["platform-error", "not-merged"],
    )
    async def test_an_unanswered_check_is_logged_noticed_and_settled(self, caplog, client):
        row = await _row()

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await _settle(client)

        assert any(record.levelname == "ERROR" and "group/core!12" in record.getMessage() for record in caplog.records)
        event = await InboxEvent.objects.aget(session_id=row.session_id)
        assert (event.kind, event.wake, event.repo_id) == (EventKind.NOTICE, False, "group/core")
        assert "couldn't check" in event.payload["text"]
        assert "`libs/core`" in event.payload["text"]
        await _assert_cleared(row)
        assert await _archived(row)

    async def test_a_row_on_another_merge_request_is_left_alone(self):
        row = await _row(merge_request_iid=13)
        client = _client(contains=False)

        await _settle(client)

        client.get_merged_commit.assert_not_called()
        await row.arefresh_from_db()
        assert (row.merge_request_iid, row.merge_pending, row.ref) == (13, True, "daiv/core")


class TestMarkAndClose:
    async def test_a_merge_marks_only_its_rows_and_enqueues_their_settling(self):
        row = await _row(merge_pending=False)
        other = await _row(merge_pending=False, merge_request_iid=13)

        with patch("sessions.tasks.settle_submodule_merge_task") as task:
            task.aenqueue = AsyncMock()
            assert await SubmoduleMerges("group/core", 12).amark_merged() == 1

        task.aenqueue.assert_awaited_once_with(repo_id="group/core", iid=12)
        await row.arefresh_from_db()
        await other.arefresh_from_db()
        assert (row.merge_pending, other.merge_pending) == (True, False)

    async def test_a_merge_no_row_holds_enqueues_nothing(self):
        with patch("sessions.tasks.settle_submodule_merge_task") as task:
            task.aenqueue = AsyncMock()
            assert await SubmoduleMerges("group/core", 12).amark_merged() == 0

        task.aenqueue.assert_not_awaited()

    async def test_a_close_without_merge_clears_the_branch_and_merge_request(self):
        row = await _row(merge_pending=False, recorded_sha="9f8e7d6c")

        assert await SubmoduleMerges("group/core", 12).aclose() == [row.session_id]

        await _assert_cleared(row)
        assert row.recorded_sha == "9f8e7d6c"
