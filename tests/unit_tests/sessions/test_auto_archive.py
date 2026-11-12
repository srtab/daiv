from unittest.mock import Mock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.auto_archive import MergeArchive
from sessions.models import (
    ArchivedMerge,
    EventKind,
    InboxEvent,
    MergeRequestOwnership,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
    SessionRepository,
)

from tests.unit_tests.sessions.conftest import amake_owned_merge_request


async def _merged(thread_id: str, iid: int = 7) -> MergeRequestOwnership:
    return await amake_owned_merge_request(
        thread_id=thread_id, merge_request_iid=iid, closed_at=timezone.now(), merged=True
    )


async def _leg(thread_id: str, parent_id: str) -> Session:
    return await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.DELEGATED_JOB, repo_id="group/repo", parent_id=parent_id
    )


@pytest.mark.django_db(transaction=True)
class TestMergeArchive:
    async def test_every_session_on_the_merge_request_is_archived(self):
        owner = await _merged("owner")
        await Session.objects.acreate(
            thread_id="review", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", merge_request_iid=7
        )
        issue = await Session.objects.acreate(
            thread_id="issue", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/repo"
        )
        await Run.objects.acreate(
            session=issue,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            merge_request_iid=7,
            status=RunStatus.SUCCESSFUL,
        )

        archived = await MergeArchive("group/repo", 7).aarchive()

        assert set(archived) == {owner.session_id, "review", "issue"}
        assert not await Session.objects.filter(archived_at__isnull=True).aexists()

    async def test_another_repository_or_merge_request_is_left_alone(self):
        other_repo = await Session.objects.acreate(thread_id="other-repo", origin=SessionOrigin.CHAT, repo_id="o/r")
        await Run.objects.acreate(
            session=other_repo, trigger_type=SessionOrigin.CHAT, repo_id="o/r", merge_request_iid=7
        )
        await Session.objects.acreate(
            thread_id="other-mr", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", merge_request_iid=8
        )

        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert not await Session.objects.filter(archived_at__isnull=False).aexists()

    async def test_a_session_that_published_on_an_extra_repository_is_archived(self):
        shared = await Session.objects.acreate(thread_id="shared", origin=SessionOrigin.CHAT, repo_id="group/main")
        await SessionRepository.objects.acreate(session=shared, position=1, repo_id="group/repo", merge_request_iid=7)

        assert await MergeArchive("group/repo", 7).aarchive() == ["shared"]

    async def test_a_leg_merging_before_its_coordinator_is_followed_by_the_coordinator(self):
        coordinator = await amake_owned_merge_request(thread_id="coord", merge_request_iid=7)
        leg = await _leg("leg", "coord")
        await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo",
            merge_request_iid=8,
            session=leg,
            source_branch="leg",
            closed_at=timezone.now(),
            merged=True,
        )

        assert await MergeArchive("group/repo", 8).aarchive() == ["leg"]

        await MergeRequestOwnership.objects.filter(pk=coordinator.pk).aupdate(closed_at=timezone.now(), merged=True)
        assert await MergeArchive("group/repo", 7).aarchive() == ["coord"]

    async def test_a_session_owning_another_open_merge_request_is_left_alone(self):
        merged = await _merged("s")
        await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo", merge_request_iid=8, session_id=merged.session_id, source_branch="other"
        )

        assert await MergeArchive("group/repo", 7).aarchive() == []

    async def test_a_coordinator_with_an_unfinished_leg_run_waits(self):
        await _merged("coord")
        leg = await _leg("leg", "coord")
        await Run.objects.acreate(
            session=leg, trigger_type=SessionOrigin.DELEGATED_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )

        assert await MergeArchive("group/repo", 7).aarchive() == []

    async def test_a_coordinator_follows_a_leg_whose_run_ended_without_a_merge_request(self):
        await _merged("coord")
        leg = await _leg("leg", "coord")
        await Run.objects.acreate(
            session=leg, trigger_type=SessionOrigin.DELEGATED_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )
        assert await MergeArchive("group/repo", 7).aarchive() == []

        await Run.objects.filter(session=leg).aupdate(status=RunStatus.SUCCESSFUL)

        assert await sync_to_async(MergeArchive.settle)("leg") == ["coord"]

    async def test_a_session_owning_another_open_merge_request_waits_for_that_merge_not_a_run(self):
        merged = await _merged("s")
        await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo", merge_request_iid=8, session_id=merged.session_id, source_branch="other"
        )
        await MergeArchive("group/repo", 7).aarchive()

        assert not (await Session.objects.aget(pk="s")).archive_pending

    async def test_a_session_with_an_unfinished_run_is_left_pending(self):
        await _merged("s")
        await Run.objects.acreate(
            session_id="s", trigger_type=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", status=RunStatus.RUNNING
        )

        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert (await Session.objects.aget(pk="s")).archive_pending

    async def test_a_session_with_a_waking_inbox_event_is_left_pending(self):
        await _merged("s")
        await InboxEvent.objects.acreate(session_id="s", kind=EventKind.MENTION, source_key="note:1", wake=True)

        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert (await Session.objects.aget(pk="s")).archive_pending

    async def test_a_merge_no_session_worked_on_records_nothing(self):
        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert not await ArchivedMerge.objects.aexists()

    async def test_a_session_no_merge_left_pending_is_not_settled(self):
        await Session.objects.acreate(thread_id="s", origin=SessionOrigin.CHAT, repo_id="group/repo")

        assert await sync_to_async(MergeArchive.settle)("s") == []

    async def test_a_coordinator_that_owns_no_merged_merge_request_stays(self):
        await Session.objects.acreate(thread_id="coord", origin=SessionOrigin.CHAT, repo_id="group/repo")
        leg = await _leg("leg", "coord")
        await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo",
            merge_request_iid=8,
            session=leg,
            source_branch="leg",
            closed_at=timezone.now(),
            merged=True,
        )

        assert await MergeArchive("group/repo", 8).aarchive() == ["leg"]

    async def test_a_redelivered_merge_archives_nothing_new(self):
        await _merged("s")
        await MergeArchive("group/repo", 7).aarchive()
        first = (await Session.objects.aget(pk="s")).archived_at

        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert (await Session.objects.aget(pk="s")).archived_at == first

    async def test_a_merge_redelivered_after_an_unarchive_archives_nothing(self):
        await _merged("s")
        await MergeArchive("group/repo", 7).aarchive()
        await Session.objects.filter(pk="s").aupdate(archived_at=None)

        assert await MergeArchive("group/repo", 7).aarchive() == []
        assert (await Session.objects.aget(pk="s")).archived_at is None

    async def test_each_archived_session_is_poked(self):
        await _merged("s")

        with patch("sessions.auto_archive.session_changed", new=Mock()) as poke:
            await MergeArchive("group/repo", 7).aarchive()

        poke.assert_called_once_with("s")
