from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from sessions.auto_archive import aarchive_merged
from sessions.models import MergeRequestOwnership, Run, RunStatus, Session, SessionOrigin, SessionRepository

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
class TestArchiveMerged:
    async def test_every_session_on_the_merge_request_is_archived(self):
        owner = await _merged("owner")
        await Session.objects.acreate(
            thread_id="review", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", merge_request_iid=7
        )
        issue = await Session.objects.acreate(
            thread_id="issue", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/repo"
        )
        await Run.objects.acreate(
            session=issue, trigger_type=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/repo", merge_request_iid=7
        )

        archived = await aarchive_merged("group/repo", 7)

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

        assert await aarchive_merged("group/repo", 7) == []
        assert not await Session.objects.filter(archived_at__isnull=False).aexists()

    async def test_a_session_that_published_on_an_extra_repository_is_archived(self):
        shared = await Session.objects.acreate(thread_id="shared", origin=SessionOrigin.CHAT, repo_id="group/main")
        await SessionRepository.objects.acreate(session=shared, position=1, repo_id="group/repo", merge_request_iid=7)

        assert await aarchive_merged("group/repo", 7) == ["shared"]

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

        assert await aarchive_merged("group/repo", 8) == ["leg"]

        await MergeRequestOwnership.objects.filter(pk=coordinator.pk).aupdate(closed_at=timezone.now(), merged=True)
        assert await aarchive_merged("group/repo", 7) == ["coord"]

    async def test_a_session_owning_another_open_merge_request_is_left_alone(self):
        merged = await _merged("s")
        await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo", merge_request_iid=8, session_id=merged.session_id, source_branch="other"
        )

        assert await aarchive_merged("group/repo", 7) == []

    async def test_a_coordinator_waits_for_its_live_legs_then_follows_the_last(self):
        await _merged("coord")
        leg = await _leg("leg", "coord")
        leg_mr = await MergeRequestOwnership.objects.acreate(
            repo_id="group/repo", merge_request_iid=8, session=leg, source_branch="leg"
        )

        assert await aarchive_merged("group/repo", 7) == []

        await MergeRequestOwnership.objects.filter(pk=leg_mr.pk).aupdate(closed_at=timezone.now(), merged=True)
        assert set(await aarchive_merged("group/repo", 8)) == {"leg", "coord"}

    async def test_a_coordinator_with_an_unfinished_leg_run_waits(self):
        await _merged("coord")
        leg = await _leg("leg", "coord")
        await Run.objects.acreate(
            session=leg, trigger_type=SessionOrigin.DELEGATED_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )

        assert await aarchive_merged("group/repo", 7) == []

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

        assert await aarchive_merged("group/repo", 8) == ["leg"]

    async def test_a_redelivered_merge_archives_nothing_new(self):
        await _merged("s")
        await aarchive_merged("group/repo", 7)
        first = (await Session.objects.aget(pk="s")).archived_at

        assert await aarchive_merged("group/repo", 7) == []
        assert (await Session.objects.aget(pk="s")).archived_at == first

    async def test_each_archived_session_is_poked(self):
        await _merged("s")

        with patch("sessions.auto_archive.asession_changed", new=AsyncMock()) as poke:
            await aarchive_merged("group/repo", 7)

        poke.assert_awaited_once_with("s")
