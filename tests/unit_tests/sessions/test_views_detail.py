from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escapejs

import pytest
from allauth.socialaccount.models import SocialAccount
from bs4 import BeautifulSoup
from sessions.conf import settings as sessions_settings
from sessions.delegation import LegRun
from sessions.executor.lock import STALE_RUN_MINUTES
from sessions.hydration import HydratedThread
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    HiddenSession,
    MergeRequestOwnership,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
    SessionRepository,
    WatchState,
)
from sessions.transcript_labels import transcript_icons

from tests.unit_tests.chat.chat_pages import render_new_chat
from tests.unit_tests.htmltree import VOID_TAGS, ElementStack
from tests.unit_tests.sessions.conftest import (
    create_archive,
    delegate_messages,
    grant_read_access,
    make_artifact,
    make_coordinator,
    make_delegating_run,
    make_leg,
)
from tests.unit_tests.sessions.inbox.conftest import make_event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "group/project", "ref": "main"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.CHAT,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _null_hydration():
    """Patch target that returns an empty, non-expired hydration."""
    return AsyncMock(return_value=HydratedThread([], False, None, None, None))


def _hydration(messages):
    """Patch target that returns the given messages, non-expired."""
    return AsyncMock(return_value=HydratedThread(messages, False, None, None, None))


# ---------------------------------------------------------------------------
# session_new (chooser) + session_new_chat (empty state)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_new_requires_login(client):
    resp = client.get(reverse("session_new"))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_session_new_chat_requires_login(client):
    # The URL split moved the empty state to its own route; it must stay login-gated too.
    resp = client.get(reverse("session_new_chat"))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_session_new_chat_renders_empty_state(member_client):
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))
    assert resp.status_code == 200
    assert resp.context["session"] is None
    assert resp.context["expired"] is False
    assert resp.context["turns"] == []


@pytest.mark.django_db
def test_the_new_chat_page_subscribes_to_no_session(member_client):
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))

    assert "url: '/api/nav/events'" in resp.content.decode()
    assert resp.context["nav_session_thread_id"] == ""


@pytest.mark.django_db
def test_session_new_chat_has_no_breadcrumb(member_client):
    """The chat page is the composer plus the transcript; "Sessions › New" only
    pushes both down, and the sidebar already links back to the list."""
    resp = member_client.get(reverse("session_new_chat"))
    assert b'data-testid="app-breadcrumb"' not in resp.content


class _Ancestors(ElementStack):
    def __init__(self, testid: str):
        super().__init__()
        self.testid = testid
        self.tags: list[str] | None = None

    def visit(self, tag, classes, attrs):
        if attrs.get("data-testid") == self.testid:
            self.tags = [ancestor for ancestor, _ in self.stack]


@pytest.mark.django_db
def test_session_new_chat_shows_the_workspace_card(member_client):
    card = str(
        BeautifulSoup(render_new_chat(member_client), "html.parser").select_one('[data-testid="workspace-card"]')
    )

    assert "Workspace" in card and "up to 5 repositories" in card
    assert "maxRepos: 5" in card and "uniqueRepos: true" in card and escapejs("daiv:chat-repo-changed") in card
    assert "Environment" in card and escapejs("daiv:env-changed") in card
    assert "(autoEnvLabel) || 'Auto'" in card


@pytest.mark.django_db
def test_the_workspace_card_is_in_the_page_at_load_not_inserted_by_alpine(member_client):
    """HTMX wires the repo popover's search input only in its page-load sweep, and never sees
    nodes Alpine inserts later, so neither the card nor the hero around it may be an ``x-if``."""
    html = render_new_chat(member_client)
    parser = _Ancestors("workspace-card")
    parser.feed(html)

    assert parser.tags is not None
    assert "template" not in parser.tags
    assert 'class="chat-hero" x-show="(!thread && turns.length === 0) || firstTurnRefused"' in html


@pytest.mark.django_db
def test_the_composer_has_no_repository_row_and_the_options_sheet_no_environment_picker(member_client):
    html = render_new_chat(member_client)

    assert 'class="chat-context"' not in html
    options = BeautifulSoup(html, "html.parser").select_one(".composer-sheet--options")
    assert "daiv:env-changed" not in str(options) and "Tools" in options.get_text()


@pytest.mark.django_db
def test_a_session_page_has_no_workspace_card(member_client, member_user):
    html = _get_detail(member_client, _create_session(user=member_user)).content.decode()

    assert 'data-testid="workspace-card"' not in html
    assert "chat-hero" not in html


@pytest.mark.django_db
def test_session_new_renders_chooser_with_both_paths(member_client):
    resp = member_client.get(reverse("session_new"))
    assert resp.status_code == 200
    content = resp.content.decode()
    # The chooser links to both destinations — the guidance lives at the fork.
    assert reverse("session_new_chat") in content
    assert reverse("runs:agent_run_new") in content
    assert "Background run" in content


# ---------------------------------------------------------------------------
# session_detail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_detail_requires_login(client, member_user):
    session = _create_session(user=member_user)
    resp = client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_detail_404_for_other_users_session(member_client, other_user):
    session = _create_session(user=other_user)
    resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_renders_for_own_session(member_client, member_user):
    session = _create_session(user=member_user)
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 200
    assert resp.context["session"] == session
    assert resp.context["expired"] is False


def _multi_repo_session(user) -> Session:
    session = _create_session(user=user, repo_id="g/app", ref="main", merge_request_iid=7)
    SessionRepository.objects.create(
        session=session,
        position=1,
        repo_id="g/mod",
        ref="feat/mod",
        merge_request_iid=3,
        merge_request_web_url="https://gl/g/mod/-/merge_requests/3",
        merge_request_title="Mod change",
        merge_request_draft=True,
        diff_stats={"lines_added": 4, "lines_removed": 9, "files_changed": 1},
    )
    return session


@pytest.mark.django_db
def test_detail_lists_every_repository_and_its_mr(member_client, member_user):
    resp = _get_detail(member_client, _multi_repo_session(member_user))

    rows = resp.context["header"]["repositories"]["rows"]
    assert rows[1] == {
        "repo_id": "g/mod",
        "ref": "feat/mod",
        "mr_url": "https://gl/g/mod/-/merge_requests/3",
        "mr_iid": 3,
        "mr_title": "Mod change",
        "mr_draft": True,
        "diff_stats": {"lines_added": 4, "lines_removed": 9, "files_changed": 1},
        "can_act": True,
        "errors": [],
    }
    assert rows[0]["repo_id"] == "g/app"


@pytest.mark.django_db
def test_each_row_says_whether_the_viewer_can_act_on_its_repository(member_client, member_user):
    with patch("sessions.page_state.runnable_repo_ids", return_value={"g/app"}):
        rows = _get_detail(member_client, _multi_repo_session(member_user)).context["header"]["repositories"]["rows"]

    assert [(r["repo_id"], r["can_act"]) for r in rows] == [("g/app", True), ("g/mod", False)]


@pytest.mark.django_db
def test_a_viewer_who_cannot_run_the_main_repository_can_act_on_none(member_client, member_user):
    with patch("sessions.page_state.runnable_repo_ids", return_value={"g/mod"}) as runnable:
        rows = _get_detail(member_client, _multi_repo_session(member_user)).context["header"]["repositories"]["rows"]

    assert [r["can_act"] for r in rows] == [False, False]
    assert [c.args[1] for c in runnable.call_args_list] == [["g/app", "g/mod"]]


@pytest.mark.django_db
def test_a_repository_without_a_branch_takes_its_default_branch_from_the_catalog(member_client, member_user):
    from codebase.models import RepositoryCatalog

    session = _create_session(user=member_user, ref="")
    SessionRepository.objects.create(session=session, position=1, repo_id="g/mod", ref="")
    SessionRepository.objects.create(session=session, position=2, repo_id="g/lib", ref="feat/lib")
    for slug, branch in (("group/project", "develop"), ("g/lib", "main")):
        RepositoryCatalog.objects.create(provider="gitlab", slug=slug, default_branch=branch, synced_at=timezone.now())

    rows = _get_detail(member_client, session).context["header"]["repositories"]["rows"]

    assert [r["ref"] for r in rows] == ["develop", "", "feat/lib"]


@pytest.mark.django_db
def test_a_branch_nobody_knows_reads_as_the_default_branch(member_client, member_user):
    card = _card(_get_detail(member_client, _create_session(user=member_user, ref="")))

    assert 'x-show="!row.ref">default branch</span>' in card


@pytest.mark.django_db
def test_detail_main_mr_links_to_the_run_url_without_a_payload(member_client, member_user):
    session = _multi_repo_session(member_user)
    _create_run(session, merge_request_web_url="https://gl/g/app/-/merge_requests/7")

    resp = _get_detail(member_client, session)

    assert resp.context["header"]["repositories"]["rows"][0]["mr_url"] == "https://gl/g/app/-/merge_requests/7"


@pytest.mark.django_db
def test_detail_shows_latest_publish_errors(member_client, member_user):
    session = _multi_repo_session(member_user)
    _create_run(session, publish_errors=[{"repo_id": "g/old", "error": "stale"}])
    _create_run(session, publish_errors=[{"repo_id": "g/mod", "error": "push rejected"}])

    rows = _get_detail(member_client, session).context["header"]["repositories"]["rows"]

    assert [(r["repo_id"], r["errors"]) for r in rows] == [("g/app", []), ("g/mod", ["push rejected"])]


@pytest.mark.django_db
def test_detail_hides_publish_errors_once_a_new_run_starts(member_client, member_user):
    session = _multi_repo_session(member_user)
    _create_run(session, publish_errors=[{"repo_id": "g/mod", "error": "push rejected"}])
    _create_run(session, status=RunStatus.RUNNING)

    rows = _get_detail(member_client, session).context["header"]["repositories"]["rows"]

    assert [r["errors"] for r in rows] == [[], []]


@pytest.mark.django_db
def test_single_repo_detail_has_one_repository_row(member_client, member_user):
    resp = _get_detail(member_client, _create_session(user=member_user))

    rows = resp.context["header"]["repositories"]["rows"]
    assert [r["repo_id"] for r in rows] == ["group/project"]


@pytest.mark.django_db
def test_a_finished_run_marks_the_session_as_ran(member_client, member_user):
    session = _multi_repo_session(member_user)
    _create_run(session, status=RunStatus.SUCCESSFUL)

    assert _get_detail(member_client, session).context["header"]["repositories"]["ran"] is True


@pytest.mark.django_db
def test_a_failed_last_run_does_not_mark_the_session_as_ran(member_client, member_user):
    session = _multi_repo_session(member_user)
    _create_run(session, status=RunStatus.SUCCESSFUL)
    _create_run(session, status=RunStatus.FAILED)
    _create_run(session, status=RunStatus.RUNNING)

    assert _get_detail(member_client, session).context["header"]["repositories"]["ran"] is False


NO_CHANGES = {"lines_added": 0, "lines_removed": 0, "files_changed": 0}


def _get_detail_with_diff_stats(client, session: Session, stats):
    hydrated = AsyncMock(return_value=HydratedThread([], False, None, stats, None))
    with (
        patch("sessions.page_state.ahydrate_thread", hydrated),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stats",
    [
        {"lines_added": 12, "lines_removed": 3, "files_changed": 2},
        {"lines_added": 0, "lines_removed": 0, "files_changed": 1},
    ],
)
def test_the_main_row_takes_the_checkpoints_diff_stats(member_client, member_user, stats):
    resp = _get_detail_with_diff_stats(member_client, _create_session(user=member_user), stats)

    assert resp.context["header"]["repositories"]["rows"][0]["diff_stats"] == stats


@pytest.mark.django_db
def test_a_publish_that_changed_nothing_has_no_change_size(member_client, member_user):
    session = _create_session(user=member_user)
    SessionRepository.objects.create(session=session, position=1, repo_id="g/mod", ref="main", diff_stats=NO_CHANGES)

    rows = _get_detail_with_diff_stats(member_client, session, NO_CHANGES).context["header"]["repositories"]["rows"]

    assert [r["diff_stats"] for r in rows] == [None, None]


@pytest.mark.django_db
def test_a_finished_run_that_changed_nothing_reads_no_changes(member_client, member_user):
    session = _create_session(user=member_user)
    _create_run(session, status=RunStatus.SUCCESSFUL)

    resp = _get_detail_with_diff_stats(member_client, session, NO_CHANGES)

    repositories = resp.context["header"]["repositories"]
    row = repositories["rows"][0]
    assert (repositories["ran"], row["mr_iid"], row["diff_stats"]) == (True, None, None)
    assert "'repo-card--quiet': isQuiet(row)" in _card(resp)


@pytest.mark.django_db
def test_the_page_subscribes_the_nav_stream_to_its_session(member_client, member_user):
    session = _create_session(user=member_user)

    resp = _get_detail(member_client, session)

    assert f"url: '/api/nav/events?session={session.thread_id}'" in resp.content.decode()


@pytest.mark.django_db
def test_the_page_seeds_the_revision(member_client, member_user):
    session = _create_session(user=member_user)

    with patch("sessions.page_state.aread_session_revision", AsyncMock(return_value=9)) as revision:
        resp = _get_detail(member_client, session)

    revision.assert_awaited_once_with(session.thread_id)
    assert resp.context["session_revision"] == 9


@pytest.mark.django_db
def test_the_page_wires_session_sync_to_revisions(member_client, member_user):
    session = _create_session(user=member_user)

    with patch("sessions.page_state.aread_session_revision", AsyncMock(return_value=7)):
        html = _get_detail(member_client, session).content.decode()

    assert "revision: 7" in html
    assert '@daiv:session-changed.window="changed($event.detail)"' in html
    assert '@daiv:live-run-ended.window="runEnded($event.detail)"' in html
    assert "session-check" not in html and "session-settled" not in html and "upNextUrl" not in html


@pytest.mark.django_db
@pytest.mark.parametrize(("redis_url", "polls"), [("", "true"), ("redis://redis:6379/0", "false")])
def test_the_page_polls_only_without_a_bus(member_client, member_user, settings, redis_url, polls):
    """Without the bus no announcement ever comes, so the page fetches on its own while the session is in flight."""
    settings.DJANGO_REDIS_URL = redis_url
    session = _create_session(user=member_user)

    with patch("sessions.page_state.aread_session_revision", AsyncMock(return_value=7)):
        html = _get_detail(member_client, session).content.decode()

    assert f"active: false, polls: {polls} }})" in html


@pytest.mark.django_db
def test_detail_seeds_the_tools_sheet_with_the_effective_selection(member_client, member_user):
    """The reloaded Tools sheet is seeded with the effective selection (defaults with the
    session's stored overrides applied), not with the raw ``mcp_overrides`` keys."""
    from mcp_connectors.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a",
        scope=MCPServer.Visibility.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://a",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="b",
        scope=MCPServer.Visibility.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://b",
        status=MCPServer.Status.ON_DEMAND,
    )
    session = _create_session(user=member_user, mcp_overrides={"b": "on"})
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 200
    # default {"a"} + override {"b": "on"} → both checked, not just the overridden name.
    assert resp.context["mcp_servers_selected"] == ["a", "b"]
    assert [(row["name"], row["is_default"]) for row in resp.context["mcp_server_rows"]] == [("a", True), ("b", False)]


@pytest.mark.django_db
def test_detail_seeds_the_slash_command_catalog(member_client, member_user):
    session = _create_session(user=member_user)
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 200
    assert any(row["name"] == "help" and row["kind"] == "command" for row in resp.context["slash_command_rows"])


@pytest.mark.django_db
def test_new_chat_seeds_the_slash_command_catalog(member_client):
    """The catalog must be set above the ``session is None`` early return: the "/" menu
    has to work on the very first message, before any thread exists."""
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))
    assert resp.status_code == 200
    assert any(row["name"] == "help" and row["kind"] == "command" for row in resp.context["slash_command_rows"])


@pytest.mark.django_db
def test_detail_with_live_checkpoint_renders_transcript(member_client, member_user):
    from langchain_core.messages import AIMessage

    session = _create_session(user=member_user)
    msg = AIMessage(content="hello from agent", id="m-1")
    tup = MagicMock(checkpoint={"channel_values": {"messages": [msg]}})

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["segments"] == [{"type": "text", "content": "hello from agent"}]


@pytest.mark.django_db
def test_detail_reconstructs_deltachannel_transcript(member_client, member_user):
    """Regression: deepagents' ``DeltaChannel`` leaves ``messages`` out of
    ``channel_values`` on non-snapshot steps, so a raw read yields an empty transcript on
    reload. The detail view must reconstruct it from the delta write history instead."""
    from langchain_core.messages import AIMessage, HumanMessage

    session = _create_session(user=member_user)
    # A live checkpoint whose channel_values has NO messages key (DeltaChannel non-snapshot).
    tup = MagicMock(checkpoint={"channel_values": {"session_id": "x"}})
    writes = [
        ("t0", "messages", [HumanMessage(content="count todos", id="h1")]),
        ("t1", "messages", [AIMessage(content="there are 18", id="a1")]),
    ]

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        saver.aget_delta_channel_history = AsyncMock(return_value={"messages": {"writes": writes}})
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["segments"] == [{"type": "text", "content": "count todos"}]
    assert turns[1]["segments"] == [{"type": "text", "content": "there are 18"}]


@pytest.mark.django_db
def test_detail_with_missing_checkpoint_flags_expired(member_client, member_user):
    session = _create_session(user=member_user)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True


@pytest.mark.django_db
def test_detail_with_an_expired_checkpoint_renders_the_archived_transcript(member_client, member_user):
    from langchain_core.messages import AIMessage, HumanMessage

    session = _create_session(user=member_user)
    messages = [HumanMessage(content="count todos", id="h1"), AIMessage(content="there are 18", id="a1")]
    create_archive(session.thread_id, 0, messages)

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert [t["role"] for t in resp.context["turns"]] == ["user", "assistant"]


@pytest.mark.django_db
def test_detail_ci_watch_placeholder_shows_watch_notice_not_expired(member_client, member_user):
    session = _create_session(
        user=member_user, origin=SessionOrigin.PIPELINE_WEBHOOK, merge_request_iid=42, watch_state=WatchState.WATCHING
    )

    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["watch_only"] is True
    content = resp.content.decode()
    assert "state has expired" not in content
    assert "!42" in content


@pytest.mark.django_db
def test_the_ci_watch_notice_hides_once_the_transcript_has_turns(member_client, member_user):
    session = _create_session(
        user=member_user, origin=SessionOrigin.PIPELINE_WEBHOOK, merge_request_iid=42, watch_state=WatchState.WATCHING
    )

    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        html = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id})).content.decode()

    notice = html[: html.index("This session tracks CI on !42")]
    assert notice[notice.rindex("<div") :].startswith('<div x-show="turns.length === 0"')


@pytest.mark.django_db
@pytest.mark.parametrize(("expired", "rendered"), [(True, "true"), (False, "false")])
def test_session_sync_knows_whether_the_page_rendered_expired(member_client, member_user, expired, rendered):
    session = _create_session(user=member_user, ref="")

    with (
        patch("sessions.page_state.aread_session_revision", AsyncMock(return_value=3)),
        patch(
            "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], expired, None, None, None))
        ),
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["expired"] is expired
    assert f"revision: 3, expired: {rendered}, active: false," in resp.content.decode()


@pytest.mark.django_db
def test_detail_expired_checkpoint_disables_composer(member_client, member_user):
    """_ahydrate returning (.., expired=True, ..) => context['expired'] is True
    and the template renders the expired notice."""
    session = _create_session(user=member_user)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # tup=None => expired
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
    # Template should contain the expired notice text
    content = resp.content.decode()
    assert "expired" in content.lower() or "state has expired" in content.lower()


@pytest.mark.django_db
def test_detail_missing_checkpoint_not_expired_while_run_in_flight(member_client, member_user):
    """A just-started background run has no checkpoint yet; that must NOT render as
    'expired'. Instead its prompt shows while the live stream takes over."""
    session = _create_session(user=member_user, ref="")  # ref="" skips the MR-payload lookup
    run = _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING, prompt="please fix X")
    Session.objects.filter(pk=session.pk).update(active_run_id=str(run.pk))

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # no checkpoint yet
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert resp.context["is_in_flight"] is True
    assert resp.context["waiting_since"] == ""
    assert [t["role"] for t in resp.context["turns"]] == ["user"]
    assert resp.context["turns"][0]["segments"][0]["content"] == "please fix X"
    assert "pending" not in resp.context["turns"][0]
    assert "has expired" not in resp.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("status", [RunStatus.READY, RunStatus.QUEUED, RunStatus.RUNNING])
def test_detail_run_not_started_shows_its_prompt_as_pending(member_client, member_user, status):
    session = _create_session(user=member_user, ref="")
    run = _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=status, prompt="please fix X")

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["waiting_since"] == run.created_at.isoformat()
    assert [(t["segments"][0]["content"], t["pending"]) for t in resp.context["turns"]] == [("please fix X", True)]
    assert f'waitingSince: "{escapejs(run.created_at.isoformat())}"' in resp.content.decode()


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_all_runs_terminal(member_client, member_user):
    """No checkpoint AND no in-flight run => genuinely expired; banner still shows."""
    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.SUCCESSFUL)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True


@pytest.mark.django_db
def test_detail_missing_checkpoint_replays_prompt_turn_when_last_run_failed(member_client, member_user):
    """A SCHEDULE run that failed before checkpointing: no transcript, but its prompt survives.
    annotate_transcript replays the prompt as a user turn and appends a run_status marker.
    The raw traceback (non-chat run) must never reach the page (§F safety guarantee)."""
    from core.constants import RUN_FAILED_MESSAGE

    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.SCHEDULE,
        status=RunStatus.FAILED,
        prompt="Triage this week's Sentry errors and open MRs.",
        error_message="git.exc.GitCommandError: clone failed",
    )

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert "failed_run" not in resp.context

    turns = resp.context["turns"]
    # The prompt is replayed as a user turn (no errored key in new design).
    prompt_turn = next(t for t in turns if t["role"] == "user")
    assert prompt_turn["segments"][0]["content"] == "Triage this week's Sentry errors and open MRs."
    assert "errored" not in prompt_turn

    # A run_status marker is appended after the user turn.
    marker = next(t for t in turns if t["role"] == "run_status")
    assert marker["status"] == "failed"
    # §F: non-chat run → generic message, raw traceback must not leak.
    assert marker["message"] == RUN_FAILED_MESSAGE

    content = resp.content.decode()
    # §F: raw traceback must never reach the page.
    assert "git.exc.GitCommandError" not in content
    # Composer stays available to retry in place.
    assert "chat-composer" in content


@pytest.mark.django_db
def test_detail_failed_run_without_prompt_shows_marker(member_client, member_user):
    """An ISSUE_WEBHOOK run that failed with no prompt: a run_status marker appears (no user
    turn, since no prompt). The raw error text (non-chat) must never reach the page (§F)."""
    from core.constants import RUN_FAILED_MESSAGE

    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        status=RunStatus.FAILED,
        prompt="",
        error_message="RuntimeError: boom",
    )

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert "failed_run" not in resp.context

    turns = resp.context["turns"]
    # No prompt → no user turn; only the run_status marker.
    assert len(turns) == 1
    assert turns[0]["role"] == "run_status"
    assert turns[0]["status"] == "failed"
    assert turns[0]["message"] == RUN_FAILED_MESSAGE

    content = resp.content.decode()
    # §F: raw traceback must never reach the page.
    assert "RuntimeError: boom" not in content


@pytest.mark.django_db
def test_detail_uses_run_status_chip_not_per_turn_error_markup(member_client, member_user):
    """Failures now render as a standalone run-status chip, not per-turn error markup.
    The chip <template> is present regardless of data; the old per-turn error markup is gone."""
    session = _create_session(user=member_user)
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    content = resp.content.decode()
    assert "chat-run-status" in content  # chip template present (Alpine <template>)
    assert "chat-turn__error" not in content  # old per-turn error markup removed
    assert "chat-turn--errored" not in content
    assert 'x-text="turn.error"' not in content  # no raw-error binding


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_last_run_succeeded(member_client, member_user):
    """A missing checkpoint whose runs all SUCCEEDED is a genuine TTL expiry — annotate_transcript
    recovers no turns (only FAILED runs synthesize a marker), so the expired banner shows."""
    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.SUCCESSFUL)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
    assert "has expired" in resp.content.decode()


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_run_stale(member_client, member_user):
    """A non-terminal run whose holder stopped heartbeating (crashed worker) past
    STALE_RUN_MINUTES is dead — it must fall through to 'expired', not pin the view
    on a permanent in-flight state. Same setup as the in-flight test above, but a
    stale ``last_active_at`` flips the outcome."""
    session = _create_session(
        user=member_user, ref="", last_active_at=timezone.now() - timedelta(minutes=STALE_RUN_MINUTES + 1)
    )
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING, prompt="please fix X")

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # no checkpoint
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    # Stale heartbeat => not in flight => expired banner, the dead run's prompt not recovered.
    assert resp.context["is_in_flight"] is False
    assert resp.context["expired"] is True
    assert resp.context["turns"] == []


@pytest.mark.django_db
def test_detail_visible_to_run_actor(member_client, member_user):
    """A webhook session (user=None) is reachable via by_owner by the user linked to the account that started it."""
    SocialAccount.objects.create(user=member_user, provider="gitlab", uid="77")
    session = _create_session(
        user=None, external_provider="gitlab", external_uid="77", origin=SessionOrigin.ISSUE_WEBHOOK
    )

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["session"] == session


@pytest.mark.django_db
def test_detail_in_flight_context(member_client, member_user):
    """is_in_flight and in_flight_ids are populated from non-terminal runs."""
    session = _create_session(user=member_user)
    run_done = _create_run(session, status=RunStatus.SUCCESSFUL)
    run_live = _create_run(session, status=RunStatus.RUNNING)

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["is_in_flight"] is True
    assert str(run_live.id) in resp.context["in_flight_ids"]
    assert str(run_done.id) not in resp.context["in_flight_ids"]


@pytest.mark.django_db
def test_detail_renders_session_live_flag(member_client, member_user):
    """``sessionLive`` reaches chat() as a JS boolean, mirroring is_in_flight.

    It is the only signal the load-time scroll has for telling a still-working
    session from a finished one (see parkViewport() in chat-stream.js), so the
    template wiring is worth pinning.
    """
    session_live = _create_session(user=member_user)
    _create_run(session_live, status=RunStatus.RUNNING)

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_live.thread_id}))

    assert "sessionLive: true" in resp.content.decode()

    session_done = _create_session(user=member_user)
    _create_run(session_done, status=RunStatus.SUCCESSFUL)

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_done.thread_id}))

    assert "sessionLive: false" in resp.content.decode()


@pytest.mark.django_db
def test_any_live_holder_is_exposed_for_streaming(member_client, member_user):
    """Every in-flight holder's id reaches the page; only a chat-only session is stoppable."""
    session_chat = _create_session(user=member_user)
    _create_run(session_chat, trigger_type=SessionOrigin.CHAT, status=RunStatus.RUNNING)
    session_chat.active_run_id = "agui-run-1"
    session_chat.save(update_fields=["active_run_id"])

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_chat.thread_id}))

    assert resp.context["live_run_id"] == "agui-run-1"
    assert resp.context["live_run_stoppable"] is True

    session_bg = _create_session(user=member_user)
    bg_run = _create_run(session_bg, trigger_type=SessionOrigin.API_JOB, status=RunStatus.RUNNING)
    session_bg.active_run_id = bg_run.id
    session_bg.save(update_fields=["active_run_id"])

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_bg.thread_id}))

    assert resp.context["live_run_id"] == str(bg_run.id)
    assert resp.context["live_run_stoppable"] is False

    session_done = _create_session(user=member_user)
    done_run = _create_run(session_done, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL)
    session_done.active_run_id = done_run.id
    session_done.save(update_fields=["active_run_id"])

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_done.thread_id}))

    assert resp.context["live_run_id"] == ""
    assert resp.context["live_run_started_at"] == ""


@pytest.mark.django_db
def test_only_the_sessions_owner_is_offered_stop(client, member_user, other_user):
    """A repo reader streams the owner's chat turn, but the cancel endpoint only stops the owner's runs."""
    session = _create_session(user=other_user, active_run_id="agui-run-1")
    _create_run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.RUNNING)
    grant_read_access(member_user, session.repo_id)
    url = reverse("session_detail", kwargs={"thread_id": session.thread_id})

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        client.force_login(member_user)
        reader = client.get(url)
        client.force_login(other_user)
        owner = client.get(url)

    assert (reader.context["live_run_id"], reader.context["live_run_stoppable"]) == ("agui-run-1", False)
    assert (owner.context["live_run_id"], owner.context["live_run_stoppable"]) == ("agui-run-1", True)


@pytest.mark.django_db
def test_live_run_started_at_seeds_the_working_timer(member_client, member_user):
    """The loader's elapsed timer counts from the run's server-side start, so the
    resume path needs that timestamp."""
    started = timezone.now() - timedelta(minutes=3)

    session_chat = _create_session(user=member_user)
    _create_run(session_chat, trigger_type=SessionOrigin.CHAT, status=RunStatus.RUNNING, started_at=started)
    session_chat.active_run_id = "agui-run-1"
    session_chat.save(update_fields=["active_run_id"])

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_chat.thread_id}))

    assert resp.context["live_run_started_at"] == started.isoformat()

    # A queued run has no started_at yet — created_at keeps the timer anchored.
    session_queued = _create_session(user=member_user)
    queued_run = _create_run(session_queued, trigger_type=SessionOrigin.CHAT, status=RunStatus.QUEUED)
    session_queued.active_run_id = "agui-run-2"
    session_queued.save(update_fields=["active_run_id"])

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_queued.thread_id}))

    assert resp.context["live_run_started_at"] == queued_run.created_at.isoformat()


@pytest.mark.django_db
def test_failed_middle_run_gets_marker_after_its_turn(member_client, member_user):
    """A CHAT run that FAILED mid-session: its run_status marker must sit immediately
    after the last assistant turn belonging to that run, before the next user turn."""
    from langchain_core.messages import AIMessage, HumanMessage

    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.FAILED, message_id="h1")
    _create_run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL, message_id="h2")

    messages = [
        HumanMessage(content="one", id="h1"),
        AIMessage(content="partial", id="a1"),
        HumanMessage(content="two", id="h2"),
        AIMessage(content="done", id="a2"),
    ]
    with patch("sessions.page_state.ahydrate_thread", _hydration(messages)):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    turns = resp.context["turns"]
    roles = [t["role"] for t in turns]
    assert "run_status" in roles
    marker_idx = roles.index("run_status")
    assert turns[marker_idx - 1]["id"] == "a1"  # marker follows the failed run's assistant turn
    assert turns[marker_idx + 1]["id"] == "h2"


@pytest.mark.django_db
def test_detail_expired_checkpoint_recovers_every_failed_run(member_client, member_user):
    """A lapsed checkpoint (no messages) with several past FAILED runs recovers each run's
    prompt + marker instead of showing a bare expired banner — the banner is suppressed
    precisely because annotate_transcript produced turns to render."""
    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.FAILED,
        error_message="Run failed. Check server logs for details.",
        prompt="first ask",
    )
    _create_run(
        session,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.FAILED,
        error_message="Run failed. Check server logs for details.",
        prompt="second ask",
    )

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False  # turns recovered → not a bare expiry
    roles = [t["role"] for t in resp.context["turns"]]
    assert roles.count("run_status") == 2
    assert "has expired" not in resp.content.decode()


# ---------------------------------------------------------------------------
# session_run_download_md
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_download_md_serves_run_result(member_client, member_user):
    """GET session_run_download_md for a SUCCESSFUL run returns markdown attachment."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="# Hello\n\nWorld", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))

    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    assert "attachment" in resp["Content-Disposition"]
    content = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") else resp.content
    text = content.decode()
    assert "Hello" in text


@pytest.mark.django_db
def test_download_md_404_for_non_successful_run(member_client, member_user):
    """Failed runs cannot be downloaded."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.FAILED, result_summary="some error", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_download_md_404_for_other_users_run(member_client, other_user):
    """Other user's run cannot be downloaded."""
    session = _create_session(user=other_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="some result")

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_download_md_404_when_no_result_summary(member_client, member_user):
    """Runs with empty result_summary return 404 — nothing to serve."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_hydrates_diff_stats_from_the_checkpoint(member_client, member_user):
    """The composer's ``+x −y`` pill has to survive a reload, so the publisher-measured
    stats come out of the checkpoint alongside the MR payload."""
    session = _create_session(user=member_user)
    stats = {"lines_added": 182, "lines_removed": 41, "files_changed": 6}
    tup = MagicMock(checkpoint={"channel_values": {"messages": [], "diff_stats": stats}})

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["diff_stats"] == stats
    assert "chat-initial-diff-stats" in resp.content.decode()


@pytest.mark.django_db
def test_detail_diff_stats_none_when_the_checkpoint_has_none(member_client, member_user):
    session = _create_session(user=member_user)
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["diff_stats"] is None


@pytest.mark.django_db
def test_detail_seeds_the_ref_pill_with_the_published_branch(member_client, member_user):
    """The pill must agree with the MR rendered beside it. ``Session.ref`` lags the published
    branch on any run that never synced it back, and the live stream moves both together.
    """
    session = _create_session(user=member_user, ref="main")
    mr = {"id": 7, "url": "https://x/7", "source_branch": "feat/published"}

    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], False, mr, None, None))
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["thread_ref"] == "feat/published"
    # Closes the loop through the template: the composer seeds ``thread.ref`` from this.
    assert 'ref: "feat/published"' in resp.content.decode()


@pytest.mark.django_db
def test_detail_ref_pill_falls_back_to_the_session_ref_without_a_merge_request(member_client, member_user):
    session = _create_session(user=member_user, ref="main")

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["thread_ref"] == "main"
    assert 'ref: "main"' in resp.content.decode()


@pytest.mark.django_db
def test_detail_context_has_no_artifacts_key(member_client, member_user):
    session = _create_session(user=member_user)
    make_artifact(_create_run(session), filename="audit.html", content=b"<p>x</p>", title="Dependency audit")

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert "artifacts" not in resp.context


@pytest.mark.django_db
def test_detail_never_renders_the_bottom_artifacts_card(member_client, member_user):
    session = _create_session(user=member_user)
    make_artifact(_create_run(session), filename="audit.html", content=b"<p>x</p>", title="Dependency audit")

    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert 'id="session-artifacts-heading"' not in resp.content.decode()


@pytest.mark.django_db
def test_detail_expired_session_keeps_the_banner_and_renders_its_artifacts(member_client, member_user):
    session = _create_session(user=member_user, ref="")
    run = _create_run(session, trigger_type=SessionOrigin.UI_JOB)
    artifact = make_artifact(run, filename="audit.html", content=b"<p>x</p>", title="Dependency audit")

    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
    assert resp.context["watch_only"] is False
    (turn,) = resp.context["turns"]
    assert turn["id"] == f"run-{run.id}-artifacts"
    assert [(s["name"], s["id"]) for s in turn["segments"]] == [("publish_artifact", f"artifact-{artifact.id}")]
    content = resp.content.decode()
    assert "has expired" in content
    assert "chat-composer" not in content


@pytest.mark.django_db
def test_detail_in_flight_run_on_an_expired_session_keeps_the_working_state(member_client, member_user):
    session = _create_session(user=member_user, ref="")
    make_artifact(_create_run(session, trigger_type=SessionOrigin.UI_JOB))
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING)

    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["expired"] is False
    assert len(resp.context["turns"]) == 1


# ---------------------------------------------------------------------------
# session top bar + details panel
# ---------------------------------------------------------------------------


def _get_detail(client, session: Session, *, merge_request=None):
    with (
        patch("sessions.page_state.ahydrate_thread", _null_hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=merge_request)),
    ):
        return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))


def _part(resp, testid: str, closing: str) -> str:
    html = resp.content.decode()
    start = html.index(f'data-testid="{testid}"')
    return html[start : html.index(closing, start)]


def _topbar(resp) -> str:
    return _part(resp, "session-topbar", "</header>")


def _details(resp) -> str:
    return _part(resp, "session-details", "</aside>")


def _section(html: str, element_id: str) -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index("</section>", start)]


def _card(resp) -> str:
    return _part(resp, "repository-card", "</article>")


class _TemplateGuards(HTMLParser):
    """The ``x-if`` of every ``<template>`` enclosing each ``data-testid``, outermost first."""

    def __init__(self):
        super().__init__()
        self.open: list[tuple[str, str | None]] = []
        self.found: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if testid := attributes.get("data-testid"):
            self.found[testid] = [condition for name, condition in self.open if name == "template" and condition]
        if tag not in VOID_TAGS:
            self.open.append((tag, attributes.get("x-if")))

    def handle_endtag(self, tag):
        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth][0] == tag:
                del self.open[depth:]
                return


def _guards(html: str, testid: str) -> list[str]:
    parser = _TemplateGuards()
    parser.feed(html)
    return parser.found[testid]


@pytest.mark.django_db
def test_detail_has_a_top_bar_and_no_breadcrumb(member_client, member_user):
    session = _create_session(user=member_user, title="Fix the login redirect")

    resp = _get_detail(member_client, session)

    assert 'x-text="title">Fix the login redirect</span>' in _topbar(resp)
    assert b'data-testid="app-breadcrumb"' not in resp.content
    assert "breadcrumbs" not in resp.context


@pytest.mark.django_db
def test_detail_top_bar_sits_outside_the_scrolling_main(member_client, member_user):
    html = _get_detail(member_client, _create_session(user=member_user)).content.decode()

    assert html.count("<main") == 1
    assert html.index('data-testid="session-topbar"') < html.index("<main")
    assert html.index("</main>") < html.index('data-testid="session-details"')


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("origin", "started_by"),
    [
        (SessionOrigin.CHAT, "Chat"),
        (SessionOrigin.UI_JOB, "UI Run"),
        (SessionOrigin.MR_WEBHOOK, "MR/PR Webhook"),
        (SessionOrigin.PIPELINE_WEBHOOK, "Pipeline Webhook"),
    ],
)
def test_detail_info_tab_shows_origin_owner_runs_and_session_id(member_client, member_user, origin, started_by):
    session = _create_session(user=member_user, origin=origin, ref="feat/login")
    _create_run(session, trigger_type=origin)

    info = _section(_details(_get_detail(member_client, session)), "session-details-info")

    assert started_by in info
    assert "avatar" in info
    assert 'x-text="runCount"' in info
    assert str(session.thread_id) in info
    assert f"copyText('{escapejs(session.thread_id)}')" in info
    assert "Created" in info and "Today, " in info


@pytest.mark.django_db
def test_detail_overview_lists_the_sessions_external_references_last(member_client, member_user):
    jira_url = "https://jira.example.com/browse/PROJ-12"
    session = _create_session(
        user=member_user, external_refs=[{"key": "PROJ-12", "url": jira_url, "provider": "jira", "relation": "closes"}]
    )

    resp = _get_detail(member_client, session)
    details = _details(resp)
    refs = BeautifulSoup(_section(details, "session-details-references"), "html.parser")

    order = [details.index(f'id="session-details-{part}"') for part in ("outputs", "references", "info")]
    assert order == sorted(order)
    assert [card["key"] for card in resp.context["header"]["references"]] == ["PROJ-12"]
    assert 'id="session-details-references" x-show="references.length"' in details
    assert refs.select_one(".details-fold__summary")["x-text"] == "references.length"
    # soupsieve 4 no longer descends into <template> from an ancestor; the ref-card
    # lives inside the Alpine <template x-for>, so query the template element directly
    # (select still reaches a template's contents when called on the template itself).
    card = refs.select_one("template").select_one("a.ref-card")
    assert card[":href"] == "ref.url || null"
    assert card["target"] == "_blank" and card["rel"] == ["noopener", "noreferrer"]


@pytest.mark.django_db
def test_detail_panel_falls_back_to_the_platform_username_without_an_owner(member_client, member_user):
    session = _create_session(origin=SessionOrigin.MR_WEBHOOK, external_username="octocat")
    grant_read_access(member_user, session.repo_id)

    assert "octocat" in _details(_get_detail(member_client, session))


@pytest.mark.django_db
def test_detail_panel_counts_the_runs(member_client, member_user):
    session = _create_session(user=member_user)
    _create_run(session)
    _create_run(session)

    resp = _get_detail(member_client, session)

    assert resp.context["header"]["run_count"] == 2
    assert b'id="session-header"' in resp.content
    assert 'x-text="runCount"' in _details(resp)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "variant", "label"),
    [
        (RunStatus.RUNNING, "running", "Running"),
        (RunStatus.SUCCESSFUL, "success", "Done"),
        (RunStatus.FAILED, "failed", "Failed"),
        (RunStatus.WAITING_INPUT, "waiting", "Needs input"),
    ],
)
def test_detail_status_is_the_latest_runs(member_client, member_user, status, variant, label):
    session = _create_session(user=member_user)
    _create_run(session, status=RunStatus.FAILED)
    _create_run(session, status=status)

    resp = _get_detail(member_client, session)

    payload = resp.context["header"]["latest_run"]
    assert (payload["label"], payload["variant"]) == (label, variant)
    assert b'id="session-header"' in resp.content
    assert f">{label}</span>" in _topbar(resp)


@pytest.mark.django_db
def test_detail_links_the_issue_an_issue_session_addresses(member_client, member_user, create_db_task_result):
    from codebase.models import RepositoryCatalog

    session = _create_session(user=member_user, origin=SessionOrigin.ISSUE_WEBHOOK)
    task = create_db_task_result()
    task.args_kwargs = {"args": [], "kwargs": {"issue_iid": 42}}
    task.save(update_fields=["args_kwargs"])
    _create_run(session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, task_result=task)
    RepositoryCatalog.objects.create(
        provider="gitlab",
        slug="group/project",
        html_url="https://gitlab.example/group/project",
        synced_at=timezone.now(),
    )

    resp = _get_detail(member_client, session)

    issue_url = 'href="https://gitlab.example/group/project/-/issues/42"'
    assert issue_url in _topbar(resp)
    assert "Open issue #42" in _topbar(resp)
    assert issue_url in _details(resp)
    assert "Issue #42" in _details(resp)


@pytest.mark.django_db
def test_detail_has_no_issue_item_for_other_origins(member_client, member_user):
    session = _create_session(user=member_user, origin=SessionOrigin.UI_JOB)
    _create_run(session, trigger_type=SessionOrigin.UI_JOB)

    resp = _get_detail(member_client, session)

    assert resp.context["header"]["issue"] is None
    assert "Open issue" not in _topbar(resp)


@pytest.mark.django_db
def test_detail_hands_ci_on_the_owned_merge_requests_to_the_page(member_client, member_user):
    session = _create_session(user=member_user, origin=SessionOrigin.CHAT)
    _create_run(session)
    MergeRequestOwnership.objects.create(
        repo_id=session.repo_id, merge_request_iid=5, session=session, source_branch="feature"
    )

    with (
        patch("sessions.header.WatchPolicy", return_value=SimpleNamespace(enabled=True, max_attempts=3)),
        patch("sessions.header.RepositoryConfig.get_config"),
    ):
        resp = _get_detail(member_client, session)

    assert [(item["merge_request_iid"], item["status"]) for item in resp.context["header"]["ci"]] == [(5, "running")]
    assert resp.context["header"]["watch"] == {
        "state": "running",
        "label": "Waiting for CI",
        "tone": "attn",
        "open": True,
    }
    assert b'id="session-header"' in resp.content


@pytest.mark.django_db
def test_a_watched_sessions_ci_shows_on_its_repository_card_not_in_the_top_bar(member_client, member_user):
    resp = _get_detail(member_client, _create_session(user=member_user, watch_state=WatchState.WATCHING))

    assert 'data-testid="session-ci"' not in _topbar(resp)
    assert 'x-for="item in ciItems(row)"' in _card(resp)


@pytest.mark.django_db
def test_detail_hands_the_latest_runs_retry_to_the_page(member_client, member_user):
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.FAILED, prompt="fix it")

    with patch("sessions.page_state.runnable_repo_ids", side_effect=lambda user, ids: set(ids)):
        resp = _get_detail(member_client, session)

    latest = resp.context["header"]["latest_run"]
    assert (latest["marker_id"], latest["retry"]) == (f"run-status-{run.id}", "fix it")


@pytest.mark.django_db
def test_detail_has_no_ci_watch_when_off(member_client, member_user):
    resp = _get_detail(member_client, _create_session(user=member_user))

    assert resp.context["header"]["watch"] is None


@pytest.mark.django_db
def test_detail_top_bar_has_no_merge_request_delegations_or_details_toggle(member_client, member_user):
    session = _create_session(user=member_user, watch_state=WatchState.WATCHING)
    resp = _get_detail(member_client, session, merge_request={"id": 5, "url": "", "draft": False})
    topbar = _topbar(resp)

    for gone in (
        'data-testid="session-mr"',
        'data-testid="session-delegations"',
        "toggleDetails()",
        'data-testid="session-ci"',
    ):
        assert gone not in topbar


@pytest.mark.django_db
def test_detail_top_bar_has_a_summary_for_narrow_screens(member_client, member_user):
    topbar = _topbar(_get_detail(member_client, _create_session(user=member_user)))

    assert 'data-testid="session-summary"' in topbar
    assert 'x-show="!wide"' in topbar
    assert "openDetails('repositories')" in topbar
    assert ':aria-expanded="detailsOpen.toString()"' in topbar
    assert ':aria-label="summaryLabel"' in topbar


@pytest.mark.django_db
def test_detail_hands_the_page_the_summary_labels(member_client, member_user):
    html = _get_detail(member_client, _create_session(user=member_user)).content.decode()

    assert "summary: 'Repositories and merge requests', ci: 'CI'" in html


@pytest.mark.django_db
def test_detail_panel_carries_usage_so_the_composer_has_no_usage_sheet(member_client, member_user):
    resp = _get_detail(member_client, _create_session(user=member_user))
    html = resp.content.decode()

    assert 'id="session-details-context"' in _details(resp)
    assert "composer-sheet--context" not in html
    assert "openDetails('context')" in html
    assert "diffInDetails: true" in html
    assert 'class="chat-context"' not in html


@pytest.mark.django_db
def test_detail_remembers_the_panel_per_user(member_client, member_user):
    html = _get_detail(member_client, _create_session(user=member_user)).content.decode()

    assert f"storageKey: 'daiv.session-details.{member_user.pk}'" in html


@pytest.mark.django_db
def test_detail_sidebar_has_no_title_and_closes_only_as_a_sheet(member_client, member_user):
    details = _details(_get_detail(member_client, _create_session(user=member_user)))

    assert "session-details__title" not in details
    assert 'x-show="!wide"' in details and "setDetails(false)" in details


@pytest.mark.django_db
def test_detail_sidebar_has_overview_and_info_tabs(member_client, member_user):
    details = _details(_get_detail(member_client, _create_session(user=member_user)))

    assert 'data-testid="session-details-tabs"' in details and 'role="tablist"' in details
    assert ">Overview<" in details and ">Info<" in details
    assert "selectTab('overview')" in details and "selectTab('info')" in details
    assert details.count('role="tabpanel"') == 2


@pytest.mark.django_db
def test_detail_overview_sections_fold(member_client, member_user):
    session = _create_session(user=member_user, external_refs=[{"key": "PROJ-1"}])
    details = _details(_get_detail(member_client, session))

    for section in ("repositories", "environment", "context", "spend", "outputs", "references"):
        assert f"toggleFold('{section}')" in details
        assert f":aria-expanded=\"(!isFolded('{section}')).toString()\"" in details
        assert f"x-show=\"!isFolded('{section}')\"" in details


@pytest.mark.django_db
def test_detail_overview_sections_keep_their_visibility_conditions(member_client, member_user):
    details = _details(_get_detail(member_client, _create_session(user=member_user)))

    assert 'x-show="contextUsage"' in _section(details, "session-details-context")
    assert 'x-show="spend"' in _section(details, "session-details-spend")
    assert 'x-show="latestArtifactsById.size"' in _section(details, "session-details-outputs")


@pytest.mark.django_db
def test_detail_environment_lists_sandbox_and_model_but_not_the_repository(member_client, member_user):
    details = _details(_get_detail(member_client, _create_session(user=member_user)))

    env = _section(details, "session-details-environment")
    assert "lockedEnvLabel" in env and "lockedAgentLabel" in env and "group/project" not in env


@pytest.mark.django_db
@pytest.mark.parametrize(("multi", "label"), [(False, "Repository"), (True, "Repositories")])
def test_a_lone_repository_names_its_section_in_the_singular(member_client, member_user, multi, label):
    session = _multi_repo_session(member_user) if multi else _create_session(user=member_user)

    repositories = _section(_details(_get_detail(member_client, session)), "session-details-repositories")

    assert f">{label}</span>" in repositories
    assert 'x-text="repositoriesSummary"' in repositories
    assert 'x-for="(row, index) in repositoryRows"' in repositories


@pytest.mark.django_db
def test_the_card_binds_exactly_the_fields_its_rows_carry(member_client, member_user):
    header = _get_detail(member_client, _multi_repo_session(member_user)).context["header"]

    card = render_to_string("sessions/_repository_card.html", {"header": header, "can_act": True})

    assert set(re.findall(r"(?<![\w/])row\.(\w+)", card)) == set(header["repositories"]["rows"][0])


@pytest.mark.django_db
def test_other_sessions_is_offered_on_the_main_card_only(member_client, member_user):
    card = _card(_get_detail(member_client, _create_session(user=member_user)))

    assert 'x-if="index === 0 && row.mr_iid"' in card
    assert f"{reverse('session_list')}?repo=group/project&amp;mr=" in card


@pytest.mark.django_db
def test_only_a_multi_repository_session_tags_its_main_card(member_client, member_user):
    card = _card(_get_detail(member_client, _multi_repo_session(member_user)))

    tag = r'x-if="repositoryRows.length > 1 && index === 0">\s*<span class="repo-card__tag">Main</span>'
    assert re.search(tag, card)


@pytest.mark.django_db
def test_a_repository_without_a_merge_request_or_a_run_shows_only_its_branch(member_client, member_user):
    resp = _get_detail(member_client, _create_session(user=member_user))
    repositories = resp.context["header"]["repositories"]
    card = _card(resp)

    assert (repositories["rows"][0]["mr_iid"], repositories["ran"]) == (None, False)
    assert _guards(card, "repository-card-menu") == ["row.mr_iid || ciItems(row).length"]
    assert 'x-show="ciItems(row).length || row.diff_stats"' in card
    assert "'repo-card--quiet': isQuiet(row)" in card


@pytest.mark.django_db
def test_a_publish_error_shows_on_its_own_card(member_client, member_user):
    card = _card(_get_detail(member_client, _multi_repo_session(member_user)))

    assert 'x-for="(error, ei) in row.errors"' in card


@pytest.mark.django_db
def test_a_merge_request_without_ci_has_no_ci_line_or_toggle(member_client, member_user):
    card = _card(_get_detail(member_client, _multi_repo_session(member_user)))

    assert _guards(card, "session-details-ci") == []
    assert 'x-for="item in ciItems(row)"' in card
    assert _guards(card, "session-ci-toggle") == ["row.mr_iid || ciItems(row).length"]
    assert 'x-for="item in row.can_act ? ciItems(row) : []"' in card
    assert "toggleCiWatch(item)" in card and ':aria-checked="item.watch_enabled.toString()"' in card


@pytest.mark.django_db
def test_a_viewer_who_cannot_run_gets_no_ci_toggle(member_client, member_user):
    session = _create_session(user=member_user)

    with patch("sessions.page_state.runnable_repo_ids", return_value=set()):
        details = _details(_get_detail(member_client, session))

    assert 'data-testid="session-ci-toggle"' not in details
    assert str(session.thread_id) in _section(details, "session-details-info")


@pytest.mark.django_db
def test_detail_has_no_transcript_repositories_box(member_client, member_user):
    assert "session-repositories" not in _get_detail(member_client, _multi_repo_session(member_user)).content.decode()


@pytest.mark.django_db
def test_session_new_chat_has_no_top_bar_or_panel(member_client):
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))

    assert resp.context["header"] is None
    assert b'data-testid="session-topbar"' not in resp.content
    assert b'data-testid="session-details"' not in resp.content
    assert b"composer-sheet--context" in resp.content
    assert b"diffInDetails: false" in resp.content
    assert b"chat-hero" in resp.content


@pytest.mark.django_db
def test_detail_offers_rename_the_ci_toggle_and_archive_to_a_runner(member_client, member_user):
    session = _create_session(user=member_user, title="Fix the login redirect")

    resp = _get_detail(member_client, session, merge_request={"id": 5, "url": "", "draft": False})
    topbar = _topbar(resp)
    html = resp.content.decode()

    assert 'data-testid="session-rename"' in topbar
    assert 'data-testid="session-title-input"' in topbar
    assert 'role="menuitemcheckbox"' in _details(resp)
    assert 'data-testid="session-ci-toggle"' in _details(resp)
    assert 'data-testid="session-archive"' in topbar
    assert 'data-testid="session-archive-dialog"' in html
    assert f"sessionUrl: '{reverse('api:session_update', kwargs={'thread_id': session.thread_id})}'" in html
    assert f"ciWatchUrl: '{reverse('api:session_ci_watch', kwargs={'thread_id': session.thread_id})}'" in html
    assert f"archiveUrl: '{reverse('api:session_archive', kwargs={'thread_id': session.thread_id})}'" in html
    assert "title: 'Fix the login redirect'" in html


@pytest.mark.django_db
def test_detail_offers_only_archive_to_a_viewer_who_cannot_run(member_client, member_user):
    session = _create_session(user=member_user, title="Fix the login redirect")

    with patch("sessions.page_state.runnable_repo_ids", return_value=set()):
        resp = _get_detail(member_client, session, merge_request={"id": 5, "url": "", "draft": False})
    topbar = _topbar(resp)

    assert 'title="Fix the login redirect">Fix the login redirect</h1>' in topbar
    for testid in ("session-rename", "session-title-input", "session-ci-toggle"):
        assert f'data-testid="{testid}"' not in topbar
    assert 'data-testid="session-archive"' in topbar
    assert b'data-testid="session-archive-dialog"' in resp.content


@pytest.mark.django_db
@pytest.mark.parametrize("archived", [False, True])
def test_detail_marks_a_session_the_viewer_archived(member_client, member_user, admin_user, archived):
    session = _create_session(user=member_user)
    HiddenSession.objects.create(session=session, user=member_user if archived else admin_user)

    resp = _get_detail(member_client, session)
    topbar = _topbar(resp)
    note = topbar[: topbar.index('data-testid="session-archived"')].rsplit("<span", 1)[1]

    assert ("x-cloak" not in note) is archived
    html = resp.content.decode()
    assert f"archived: {'true' if archived else 'false'}," in html
    assert 'x-show="archived" data-testid="composer-archived"' in html
    assert 'x-show="!archived"' in html
    item = topbar[topbar.index('data-testid="session-archive"') :]
    assert (">Unarchive</span>" in item) is archived


@pytest.mark.django_db
def test_detail_and_new_chat_embed_the_transcript_labels(member_client, member_user):
    session = _create_session(user=member_user)
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        detail = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
        new_chat = member_client.get(reverse("session_new_chat"))
    assert 'id="chat-transcript-labels"' in detail.content.decode()
    assert 'id="chat-transcript-labels"' in new_chat.content.decode()
    assert detail.context["transcript_labels_payload"]["icons"] == transcript_icons()
    assert detail.context["transcript_labels_payload"]["leg_states"] == LegRun.outcome_states()


def _detail_html(client, session: Session) -> str:
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id})).content.decode()


@pytest.mark.django_db
def test_session_page_loads_the_transcript_scripts_in_order(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))
    names = ("transcript-labels", "tool-renderers", "transcript-blocks", "chat-stream")
    order = [html.index(f"chat/js/{name}.js") for name in names]
    assert order == sorted(order)


@pytest.mark.django_db
def test_session_page_renders_blocks_not_turns(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))
    assert 'x-for="block in runBlocks"' in html


@pytest.mark.django_db
def test_session_page_marks_each_runs_ending_and_step_rows(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))
    assert "data-run-ending" in html and 'class="step-row"' in html


@pytest.mark.django_db
def test_session_page_puts_a_folded_runs_closing_question_below_its_cards(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))
    assert "block.endingText, block.cards, block.endingTail]" in html


@pytest.mark.django_db
def test_session_page_renders_a_delegated_jobs_card_in_a_runs_items(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))

    card = html.index("x-if=\"item.kind === 'delegated'\"", html.index('x-for="item in list"'))
    assert card < html.index('x-for="(p, pi) in block.publish"')
    assert 'data-testid="delegated-card"' in html[card:]


def _labels_payload(html: str) -> dict:
    match = re.search(r'<script id="chat-transcript-labels" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("setting", "debug", "expected"),
    [(None, True, True), (None, False, False), (True, False, True), (False, True, False)],
)
def test_show_raw_follows_the_setting_then_debug(
    member_client, member_user, settings, monkeypatch, setting, debug, expected
):
    monkeypatch.setattr(sessions_settings, "SHOW_RAW_TOOL_DATA", setting)
    settings.DEBUG = debug
    session = _create_session(user=member_user)
    assert _labels_payload(_detail_html(member_client, session))["show_raw"] is expected
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        new_chat = member_client.get(reverse("session_new_chat")).content.decode()
    assert _labels_payload(new_chat)["show_raw"] is expected


@pytest.mark.django_db
def test_the_labels_payload_names_the_session_page_path(member_client, member_user):
    payload = _labels_payload(_detail_html(member_client, _create_session(user=member_user)))
    assert payload["session_path"] == reverse("session_detail", args=["x"]).replace("/x/", "/{thread_id}/")


@pytest.mark.django_db
def test_step_rows_show_an_mcp_servers_own_icon_as_an_image(member_client, member_user):
    html = _detail_html(member_client, _create_session(user=member_user))
    assert '<img :src="step.row.iconSrc" alt="" width="14" height="14" referrerpolicy="no-referrer">' in html


@pytest.mark.django_db
def test_detail_has_no_publish_chip_template(member_client, member_user):
    assert "chat-phase" not in _detail_html(member_client, _create_session(user=member_user))


SIBLING_OPENED = {
    "status": "opened",
    "repository": "srtab/daiv",
    "merge_request_iid": 101,
    "merge_request_web_url": "https://git/srtab/daiv/-/merge_requests/101",
    "merge_request_title": "docs: tagline",
    "files_changed": 1,
    "lines_added": 3,
    "lines_removed": 0,
    "error_kind": None,
}


def _publish_markup(client, session: Session) -> str:
    content = _detail_html(client, session)
    return content[content.index("block.endingTail") : content.index("block.statuses")]


def _published_turns(client, session: Session):
    with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
        resp = client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    return [turn for turn in resp.context["turns"] if turn.get("publish")]


@pytest.mark.django_db
def test_detail_context_lists_the_main_repository_then_the_extra_ones_in_the_last_turns_publish(
    member_client, member_user
):
    session = _create_session(user=member_user)
    _create_run(
        session,
        publish_status="pushed",
        merge_request_iid=7,
        merge_request_title="Main title",
        merge_request_web_url="https://git/group/project/-/merge_requests/7",
        repo_publishes=[SIBLING_OPENED],
    )

    (turn,) = _published_turns(member_client, session)

    main, extra = turn["publish"]
    assert (main["repository"], main["sibling"]) == ("group/project", False)
    assert (extra["repository"], extra["sibling"], extra["merge_request_iid"]) == ("srtab/daiv", True, 101)
    assert (extra["merge_request_title"], extra["merge_request_web_url"]) == (
        "docs: tagline",
        "https://git/srtab/daiv/-/merge_requests/101",
    )


@pytest.mark.django_db
def test_detail_context_has_no_publish_entries_for_a_run_that_published_nothing(member_client, member_user):
    session = _create_session(user=member_user)
    _create_run(session)

    assert _published_turns(member_client, session) == []


@pytest.mark.django_db
def test_detail_renders_the_published_label_once_and_only_when_there_are_publish_entries(member_client, member_user):
    markup = _publish_markup(member_client, _create_session(user=member_user))

    assert markup.count('class="publish-rows__label"') == 1
    assert markup.index("block.publish.length") < markup.index("publish-rows__label")
    assert markup.index("publish-rows__label") < markup.index('x-for="(p, pi) in block.publish"')


@pytest.mark.django_db
def test_detail_renders_publish_rows_between_the_ending_tail_and_the_statuses(member_client, member_user):
    content = _detail_html(member_client, _create_session(user=member_user))

    assert content.index("block.endingTail") < content.index("block.publish") < content.index("block.statuses")


@pytest.mark.django_db
def test_detail_renders_each_publish_entry_as_a_reminder_row(member_client, member_user):
    markup = _publish_markup(member_client, _create_session(user=member_user))

    assert 'class="chat-inbox-event"' in markup
    assert 'data-testid="publish-row"' in markup
    assert ':data-status="p.status"' in markup
    assert "'chat-inbox-event--failed': publishFailed(p)" in markup
    assert "'chat-inbox-event--published': !publishFailed(p)" in markup
    assert '<span class="chat-inbox-event__summary" :title="p.merge_request_title"' in markup
    assert 'x-text="p.repository"' in markup
    assert 'x-text="p.merge_request_title"' in markup
    assert markup.count(':href="p.merge_request_web_url || null"') == 1
    assert "Open ↗" not in markup
    assert "publish_files" not in markup


@pytest.mark.django_db
def test_detail_announces_every_failed_row_and_keeps_the_retry_on_the_main_repositorys_only(member_client, member_user):
    markup = _publish_markup(member_client, _create_session(user=member_user))

    assert ":role=\"publishFailed(p) ? 'status' : null\"" in markup
    assert ":aria-live=\"publishFailed(p) ? 'polite' : null\"" in markup
    assert ":data-run-ending=\"publishFailed(p) && !p.sibling && !block.items.length ? '' : false\"" in markup
    retry = markup.index('data-testid="run-retry"')
    assert markup.rindex('x-if="!p.sibling"', 0, retry) > markup.index('data-testid="publish-row"')


@pytest.mark.parametrize("name", ["session_detail.html", "_run_item.html", "_up_next.html"])
def test_templates_call_the_label_helper_that_matches_the_label_shape(name):
    import re
    from pathlib import Path

    from sessions.transcript_labels import transcript_labels

    labels = transcript_labels()
    source = (Path(__file__).resolve().parents[3] / "daiv/sessions/templates/sessions" / name).read_text()
    calls = re.findall(r"TranscriptLabels\.(tn?)\(\s*'(\w+)'", source)

    assert calls or name == "_run_item.html"
    for helper, key in calls:
        assert key in labels, key
        assert isinstance(labels[key], list) == (helper == "tn"), f"{helper}('{key}') does not match its label shape"


@pytest.mark.django_db
class TestUpNext:
    def _get(self, client, session):
        with patch("sessions.page_state.ahydrate_thread", _null_hydration()):
            return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    @pytest.mark.parametrize("viewer", ["owner", "reader"])
    def test_owner_and_repo_readers_get_up_next_in_the_header(self, member_client, member_user, other_user, viewer):
        owner = member_user if viewer == "owner" else other_user
        session = _create_session(user=owner)
        if viewer == "reader":
            grant_read_access(member_user, session.repo_id)
        next_run = _create_run(session, status=RunStatus.QUEUED, user=owner)
        chat = make_event(session, EventKind.CHAT_MESSAGE, run=next_run, author=owner)
        comment = make_event(session, EventKind.COMMENT)

        up_next = self._get(member_client, session).context["header"]["up_next"]

        (group,) = up_next["groups"]
        assert [bubble["id"] for bubble in group["bubbles"]] == [f"inbox-{chat.pk}"]
        assert [line["id"] for line in group["told"]] == [f"inbox-{comment.pk}"]
        assert group["is_viewer"] is (viewer == "owner")

    def test_nothing_pending_is_no_up_next(self, member_client, member_user):
        assert self._get(member_client, _create_session(user=member_user)).context["header"]["up_next"] is None

    def test_a_failed_event_is_a_timeline_line(self, member_client, member_user):
        session = _create_session(user=member_user)
        failed = make_event(session, EventKind.MENTION, state=EventState.FAILED, failure=EventFailure.RETRIES_EXHAUSTED)

        turns = self._get(member_client, session).context["turns"]

        (entry,) = [turn for turn in turns if turn["role"] == "inbox_event"]
        assert (entry["id"], entry["failure_label"]) == (f"inbox-{failed.pk}", "The runs that took it kept failing")

    def test_an_inbox_run_opens_with_the_events_it_took(self, member_client, member_user):
        session = _create_session(user=member_user)
        run = _create_run(session, prompt="Fix it")
        chat = make_event(session, EventKind.CHAT_MESSAGE, run=run, state=EventState.HANDLED, author=member_user)

        turns = self._get(member_client, session).context["turns"]

        anchor = next(turn for turn in turns if turn.get("run_id") == str(run.pk))
        assert [bubble["id"] for bubble in anchor["opening"]] == [f"inbox-{chat.pk}"]

    def test_the_page_takes_a_message_queued_while_busy(self, member_client, member_user):
        session = _create_session(user=member_user)

        html = self._get(member_client, session).content.decode()

        assert '@daiv:up-next-provisional.window="addProvisional($event.detail)"' in html

    def test_the_panel_is_gone(self, member_client, member_user):
        session = _create_session(user=member_user)
        make_event(session, EventKind.CHAT_MESSAGE)

        html = self._get(member_client, session).content.decode()

        assert 'data-testid="session-inbox"' not in html
        assert 'data-testid="session-queued"' not in html


@pytest.mark.django_db
def test_detail_shows_delegated_jobs_current_state(member_client, member_user):
    coordinator = make_coordinator(member_user)
    batch = uuid.uuid4()
    make_leg(coordinator, batch, status=RunStatus.FAILED)
    messages = delegate_messages(batch)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration(messages)),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": coordinator.thread_id}))

    segment = next(s for t in resp.context["turns"] for s in t["segments"] if s.get("name") == "delegate_jobs")
    assert [leg["state"] for leg in segment["legs"]] == ["failed"]


def _header_payload(resp) -> dict:
    html = resp.content.decode()
    return json.loads(re.search(r'<script id="session-header" type="application/json">(.*?)</script>', html, re.S)[1])


@pytest.mark.django_db
def test_a_coordinator_page_has_the_delegations_button_and_details_section(member_client, member_user):
    coordinator = make_coordinator(member_user)
    make_delegating_run(coordinator, member_user)
    make_leg(coordinator, uuid.uuid4(), status=RunStatus.FAILED)

    resp = _get_detail(member_client, coordinator)

    assert 'id="session-details-delegations"' in _details(resp)
    assert _header_payload(resp)["delegations"]["total"] == 1


@pytest.mark.django_db
def test_delegated_jobs_sit_in_the_info_tab(member_client, member_user):
    coordinator = make_coordinator(member_user)
    make_delegating_run(coordinator, member_user)
    make_leg(coordinator, uuid.uuid4(), status=RunStatus.FAILED)

    details = _details(_get_detail(member_client, coordinator))

    assert details.index('id="session-details-info"') < details.index('id="session-details-delegations"')


@pytest.mark.django_db
def test_a_delegated_jobs_page_links_the_session_that_delegated_it(member_client, member_user):
    coordinator = make_coordinator(member_user, title="Parent work")
    child = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL).session
    parent_url = reverse("session_detail", kwargs={"thread_id": coordinator.thread_id})

    resp = _get_detail(member_client, child)

    topbar, details = _topbar(resp), _details(resp)
    assert "Delegated by" in topbar and f'href="{parent_url}"' in topbar and "Parent work" in topbar
    assert "Delegated by" in details and f'href="{parent_url}"' in details
    assert topbar.index("Delegated by") < topbar.index("session-topbar__title")


@pytest.mark.django_db
def test_a_delegated_jobs_page_hides_a_parent_the_viewer_cannot_see(member_client, member_user, other_user):
    coordinator = make_coordinator(other_user, title="Someone else's work")
    child = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL).session
    grant_read_access(member_user, child.repo_id)

    resp = _get_detail(member_client, child)

    html = resp.content.decode()
    assert "Delegated by" not in html and "Someone else" not in html
    assert "Delegated Run" in _details(resp)
