import uuid
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.db import Error as DatabaseError
from django.db import OperationalError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import EventType
from notifications.models import Notification
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin

from accounts.context_processors import (
    _resolve_active_section,
    feed_unread_attention_count,
    feed_unread_count,
    nav,
    running_jobs_count,
)
from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


@pytest.mark.django_db
class TestNavContextProcessor:
    def test_returns_empty_for_anonymous_user(self):
        request = RequestFactory().get("/dashboard/")
        request.user = AnonymousUser()
        assert nav(request) == {}

    def test_returns_zero_running_jobs_when_none(self, user):
        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 0
        assert out["nav_active_section"] == ""

    def test_counts_only_running_jobs_owned_by_user(self, user, db):
        from sessions.models import Session

        session = Session.objects.create(
            thread_id="test-thread-1", origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=user
        )
        Run.objects.create(
            session=session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=user
        )
        Run.objects.create(
            session=session,
            status=RunStatus.SUCCESSFUL,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=user,
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        session2 = Session.objects.create(
            thread_id="test-thread-2", origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=other
        )
        Run.objects.create(
            session=session2,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=other,
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1

    def test_resolves_active_section_from_url_name(self, db):
        user_obj = User.objects.create_user(username="charlie", email="c@test.com", password="x123456789")  # noqa: S106
        client = Client()
        client.force_login(user_obj)
        response = client.get(reverse("dashboard"))
        assert response.context["nav_active_section"] == "dashboard"

    def test_running_jobs_falls_back_to_zero_on_database_error(self, user, mocker):
        # A transient DB failure should log-and-degrade the badge rather than crash rendering.
        failing_qs = mocker.MagicMock()
        failing_qs.filter.return_value.count.side_effect = DatabaseError("connection lost")
        mocker.patch("sessions.models.RunManager.visible_to", return_value=failing_qs)
        request = RequestFactory().get("/dashboard/")
        request.user = user
        assert running_jobs_count(request, user) == 0

    def test_running_jobs_memoizes_on_request(self, user, db, mocker):
        # Second call on the same request must not re-query the database.
        request = RequestFactory().get("/dashboard/")
        request.user = user
        assert running_jobs_count(request, user) == 0

        spy = mocker.patch("sessions.models.RunManager.visible_to")
        assert running_jobs_count(request, user) == 0
        spy.assert_not_called()

    def test_counts_running_jobs_on_readable_repos(self, user, db):
        from django.utils import timezone

        from allauth.socialaccount.models import SocialAccount

        from codebase.base import RepoAccessLevel
        from codebase.models import RepositoryAccess

        SocialAccount.objects.create(user=user, provider="gitlab", uid="alice-1")
        RepositoryAccess.objects.create(
            provider="gitlab",
            uid="alice-1",
            username=user.username,
            repo_id="daiv/api",
            access_level=RepoAccessLevel.READ,
            synced_at=timezone.now(),
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, user=other, repo_id="daiv/api"
        )
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.MCP_JOB,
            status=RunStatus.RUNNING,
            user=other,
            repo_id="daiv/api",
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1  # bob's running job, on a repo alice can read


class TestResolveActiveSection:
    def test_returns_empty_when_resolver_match_is_missing(self):
        request = RequestFactory().get("/")
        request.resolver_match = None
        assert _resolve_active_section(request) == ""

    def test_returns_empty_for_view_name_outside_known_sections(self):
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "account_login"})()
        assert _resolve_active_section(request) == ""

    def test_namespaced_view_name_disambiguates_colliding_url_names(self):
        # ``sandbox_envs:list`` and ``notifications:list`` share ``url_name == "list"``;
        # only the sandbox-envs namespace should resolve to the ``sandbox_envs`` section.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "sandbox_envs:list"})()
        assert _resolve_active_section(request) == "sandbox_envs"

        request.resolver_match = type("Match", (), {"view_name": "notifications:list"})()
        assert _resolve_active_section(request) == ""

    def test_namespaced_agent_run_new_highlights_sessions_section(self):
        # The "Start a run" page lives in the ``runs`` namespace, so its view_name is
        # prefixed. The sessions section must list the prefixed name to highlight it.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "runs:agent_run_new"})()
        assert _resolve_active_section(request) == "sessions"

    def test_nav_section_override_wins(self):
        # A view may pin the section explicitly — needed where one URL serves rows of
        # several sections (e.g. mcp_servers:edit renders global AND personal rows).
        request = RequestFactory().get("/dashboard/mcp-servers/1/edit/")
        request.nav_section_override = "mcp_servers_global"
        assert _resolve_active_section(request) == "mcp_servers_global"


# ---------------------------------------------------------------------------
# Story 2.4 — Feed unread-attention count (Option 1)
# ---------------------------------------------------------------------------


@pytest.fixture
def rf():
    return RequestFactory()


def _make_feed_run(user, *, envelope_status=None, repo_id="group/project"):
    """Build a SCHEDULE run for ``user``, optionally with a ``RunEnvelope`` of the given status."""
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id=repo_id, user=user
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id=repo_id,
        status=RunStatus.SUCCESSFUL,
        user=user,
        finished_at=timezone.now(),
    )
    if envelope_status is not None:
        RunEnvelope.objects.create(run=run, status=envelope_status)
    return run


def _feed_notif(user, source_id, *, read=False):
    """Create a RUN_FEED notification for ``user`` pointing at ``source_id`` (a run pk or orphan)."""
    return Notification.objects.create(
        recipient=user,
        event_type=EventType.RUN_FEED,
        source_type="sessions.Run",
        source_id=str(source_id),
        subject="nightly",
        body="",
        link_url="/",
        read_at=timezone.now() if read else None,
    )


@pytest.mark.django_db
class TestFeedUnreadAttentionCount:
    """Option 1 — count only unread RUN_FEED rows whose run exists and is not all-clear."""

    def test_all_clear_is_excluded(self, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        _feed_notif(member_user, run.pk)
        assert feed_unread_attention_count(member_user) == 0

    def test_orphan_deleted_run_is_excluded(self, member_user):
        # A RUN_FEED row whose Run was deleted out from under it — must not count.
        _feed_notif(member_user, uuid.uuid4())
        assert feed_unread_attention_count(member_user) == 0

    def test_classifying_is_counted(self, member_user):
        # Run exists but has no envelope yet — needs attention until it resolves.
        run = _make_feed_run(member_user, envelope_status=None)
        _feed_notif(member_user, run.pk)
        assert feed_unread_attention_count(member_user) == 1

    def test_needs_attention_is_counted(self, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _feed_notif(member_user, run.pk)
        assert feed_unread_attention_count(member_user) == 1

    def test_found_issues_is_counted(self, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.FOUND_ISSUES)
        _feed_notif(member_user, run.pk)
        assert feed_unread_attention_count(member_user) == 1

    def test_failed_is_counted(self, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED)
        _feed_notif(member_user, run.pk)
        assert feed_unread_attention_count(member_user) == 1

    def test_read_row_is_excluded(self, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _feed_notif(member_user, run.pk, read=True)
        assert feed_unread_attention_count(member_user) == 0

    def test_mixed_numeric_value(self, member_user):
        # Two rows need attention: an unread needs-attention run and an unread classifying run.
        # Three do not count: an all-clear run, a deleted-run orphan, and a read failed run.
        _feed_notif(member_user, _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION).pk)
        _feed_notif(member_user, _make_feed_run(member_user, envelope_status=None).pk)
        _feed_notif(member_user, _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR).pk)
        _feed_notif(member_user, uuid.uuid4())
        _feed_notif(member_user, _make_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED).pk, read=True)
        assert feed_unread_attention_count(member_user) == 2

    def test_is_per_user(self, member_user, admin_user):
        # Both users hold a Feed row for the SAME attention run; the count is independent per recipient.
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _feed_notif(member_user, run.pk)
        _feed_notif(admin_user, run.pk)
        assert feed_unread_attention_count(member_user) == 1
        assert feed_unread_attention_count(admin_user) == 1
        Notification.objects.filter(recipient=member_user, source_id=str(run.pk)).update(read_at=timezone.now())
        assert feed_unread_attention_count(member_user) == 0
        assert feed_unread_attention_count(admin_user) == 1


@pytest.mark.django_db
class TestFeedUnreadCount:
    """The context-processor wrapper — guards mirror ``unread_notification_count``."""

    def test_returns_count_for_authenticated_user(self, rf, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _feed_notif(member_user, run.pk)
        request = rf.get("/")
        request.user = member_user
        assert feed_unread_count(request) == {"feed_unread_count": 1}

    def test_returns_empty_for_anonymous_user(self, rf):
        request = rf.get("/")
        request.user = AnonymousUser()
        assert feed_unread_count(request) == {}

    def test_returns_zero_on_database_error(self, rf, member_user):
        request = rf.get("/")
        request.user = member_user
        with patch.object(Notification.objects, "filter", side_effect=OperationalError("connection refused")):
            result = feed_unread_count(request)
        # The count is lazy; the equality comparison forces evaluation, which trips the guard.
        assert result == {"feed_unread_count": 0}

    def test_malformed_source_id_degrades_to_zero(self, rf, member_user):
        # A RUN_FEED row with a non-UUID ``source_id`` makes the ``pk__in`` UUID filter raise
        # ValueError/ValidationError; the broadened guard (P6) must degrade to 0, not 500 the page.
        _feed_notif(member_user, "not-a-uuid")
        request = rf.get("/")
        request.user = member_user
        assert feed_unread_count(request) == {"feed_unread_count": 0}

    def test_bell_mark_all_read_leaves_feed_count(self, member_user):
        # AC6 — the bell's carved mark-all-read (exclude RUN_FEED) must not clear the Feed count.
        Notification.objects.create(
            recipient=member_user, event_type=EventType.JOB_FINISHED, subject="s", body="b", link_url="/"
        )
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _feed_notif(member_user, run.pk)
        Notification.mark_all_read_for(member_user, exclude_event_types=(EventType.RUN_FEED,))
        assert feed_unread_attention_count(member_user) == 1
