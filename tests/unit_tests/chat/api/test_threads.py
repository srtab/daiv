import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

import pytest

from accounts.models import User
from chat.api.threads import STALE_RUN_MINUTES, ChatThreadService, _extract_first_user_message
from chat.models import ChatThread


def _fake_input(messages, *, role="user"):
    return SimpleNamespace(messages=[SimpleNamespace(role=role, content=c) for c in messages])


def test_extract_first_user_message_empty_returns_empty_string():
    assert _extract_first_user_message(_fake_input([])) == ""


def test_extract_first_user_message_skips_non_string_content():
    # AG-UI supports list-of-blocks content for multimodal messages; they shouldn't be used
    # as a title. We fall through to the next string-typed message.
    payload = _fake_input([[{"type": "text", "text": "hi"}], "fallback title"])
    assert _extract_first_user_message(payload) == "fallback title"


def test_extract_first_user_message_skips_whitespace_only():
    assert _extract_first_user_message(_fake_input(["   \n\t", "actual content"])) == "actual content"


def test_extract_first_user_message_returns_first_non_empty_string():
    assert _extract_first_user_message(_fake_input(["first", "second"])) == "first"


def test_extract_first_user_message_skips_non_user_roles():
    # Title should be derived from a human/user message, never from an assistant
    # bootstrap message that happened to land in input_data.messages first.
    msgs = SimpleNamespace(
        messages=[
            SimpleNamespace(role="assistant", content="Hi! How can I help?"),
            SimpleNamespace(role="user", content="actual ask"),
        ]
    )
    assert _extract_first_user_message(msgs) == "actual ask"


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_updates_when_branch_changed():
    user = await User.objects.acreate_user(username="u-ref-1", email="ref1@x.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-ref-1", user=user, repo_id="a/b", ref="feature-x")

    await ChatThreadService.persist_ref("t-ref-1", "feature-x", SimpleNamespace(source_branch="feature-y"))

    refreshed = await ChatThread.objects.aget(thread_id="t-ref-1")
    assert refreshed.ref == "feature-y"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_noop_when_branch_unchanged():
    with patch("chat.api.threads.ChatThread.objects.filter") as filter_mock:
        await ChatThreadService.persist_ref("t-ref-2", "feature-x", SimpleNamespace(source_branch="feature-x"))

    filter_mock.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_noop_when_no_mr_captured():
    with patch("chat.api.threads.ChatThread.objects.filter") as filter_mock:
        await ChatThreadService.persist_ref("t-ref-3", "feature-x", None)

    filter_mock.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_try_claim_run_succeeds_on_free_slot():
    user = await User.objects.acreate_user(username="u-claim-1", email="c1@x.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-claim-1", user=user, repo_id="a/b", ref="main")

    assert await ChatThreadService.try_claim_run("t-claim-1", "r-1") is True

    refreshed = await ChatThread.objects.aget(thread_id="t-claim-1")
    assert refreshed.active_run_id == "r-1"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_try_claim_run_fails_on_held_slot():
    user = await User.objects.acreate_user(username="u-claim-2", email="c2@x.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(
        thread_id="t-claim-2", user=user, repo_id="a/b", ref="main", active_run_id="r-existing"
    )

    assert await ChatThreadService.try_claim_run("t-claim-2", "r-new") is False

    refreshed = await ChatThread.objects.aget(thread_id="t-claim-2")
    # Loser does not overwrite the winner's run_id.
    assert refreshed.active_run_id == "r-existing"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_try_claim_run_concurrent_calls_yield_exactly_one_winner():
    # Direct regression test for the TOCTOU fix in commit dde32f93. The whole
    # point of the conditional UPDATE is that two simultaneous claims can't
    # both succeed; this asserts the protocol holds when the calls overlap.
    user = await User.objects.acreate_user(username="u-claim-3", email="c3@x.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-claim-3", user=user, repo_id="a/b", ref="main")

    results = await asyncio.gather(
        ChatThreadService.try_claim_run("t-claim-3", "r-A"), ChatThreadService.try_claim_run("t-claim-3", "r-B")
    )
    assert sorted(results) == [False, True]

    refreshed = await ChatThread.objects.aget(thread_id="t-claim-3")
    assert refreshed.active_run_id in ("r-A", "r-B")
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_release_run_clears_slot_and_reopens_for_claim():
    user = await User.objects.acreate_user(username="u-rel", email="rel@x.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-rel", user=user, repo_id="a/b", ref="main", active_run_id="r-old")

    await ChatThreadService.release_run("t-rel", "r-old")
    refreshed = await ChatThread.objects.aget(thread_id="t-rel")
    assert refreshed.active_run_id is None

    # Next claim succeeds — the slot is genuinely free, not just blanked.
    assert await ChatThreadService.try_claim_run("t-rel", "r-next") is True
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_release_run_does_not_clear_other_holders_slot():
    """Stale `finally` from a cancelled run must not stomp a freshly-claimed slot."""
    user = await User.objects.acreate_user(username="u-rel-mismatch", email="chat@example.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-rel-x", user=user, repo_id="a/b", ref="main", active_run_id="r-fresh")

    # Stale streamer's finally tries to release with the OLD run_id.
    await ChatThreadService.release_run("t-rel-x", "r-stale")

    refreshed = await ChatThread.objects.aget(thread_id="t-rel-x")
    assert refreshed.active_run_id == "r-fresh"  # untouched
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_try_claim_run_takes_over_stale_slot():
    """Worker crash leaves active_run_id set; after the heartbeat window expires
    a fresh claim succeeds. Without this the thread would be permanently locked.
    """
    user = await User.objects.acreate_user(username="u-stale", email="owner@example.com", password="x")  # noqa: S106
    stale_at = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES + 1)
    await ChatThread.objects.acreate(thread_id="t-stale", user=user, repo_id="a/b", ref="main", active_run_id="r-dead")
    # auto_now would clobber the stale timestamp — force it via aupdate.
    await ChatThread.objects.filter(thread_id="t-stale").aupdate(last_active_at=stale_at)

    assert await ChatThreadService.try_claim_run("t-stale", "r-new") is True
    refreshed = await ChatThread.objects.aget(thread_id="t-stale")
    assert refreshed.active_run_id == "r-new"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_heartbeat_only_bumps_when_caller_holds_slot():
    """Delayed heartbeat from a previous run must not keep a stolen slot alive."""
    user = await User.objects.acreate_user(username="u-hb", email="i@example.com", password="x")  # noqa: S106
    await ChatThread.objects.acreate(thread_id="t-hb", user=user, repo_id="a/b", ref="main", active_run_id="r-current")
    old_timestamp = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES + 5)
    await ChatThread.objects.filter(thread_id="t-hb").aupdate(last_active_at=old_timestamp)

    # Stale run heartbeats — should be a no-op because it doesn't hold the slot.
    await ChatThreadService.heartbeat("t-hb", "r-stale")
    refreshed = await ChatThread.objects.aget(thread_id="t-hb")
    assert (timezone.now() - refreshed.last_active_at).total_seconds() > STALE_RUN_MINUTES * 60

    # Real holder bumps successfully.
    await ChatThreadService.heartbeat("t-hb", "r-current")
    refreshed = await ChatThread.objects.aget(thread_id="t-hb")
    assert (timezone.now() - refreshed.last_active_at).total_seconds() < 5
    await user.adelete()
