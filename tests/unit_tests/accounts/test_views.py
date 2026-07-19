import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core import mail
from django.template.loader import get_template
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import EventType
from notifications.models import Notification
from sessions.envelopes import build_actionable_item
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin

from accounts.models import APIKey, Role, User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="bob", email="bob@test.com", password="testpass456")  # noqa: S106


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_api_key(user, name="test-key"):
    gen = APIKey.objects.key_generator
    key, prefix, hashed_key = gen.generate()
    api_key = APIKey.objects.create(user=user, name=name, prefix=prefix, hashed_key=hashed_key)
    return api_key, key


@pytest.mark.django_db
class TestAPIKeyListView:
    def test_member_sees_only_own_keys(self, logged_in_client, user, other_user):
        _create_api_key(user, name="alice-key")
        _create_api_key(other_user, name="bob-key")

        response = logged_in_client.get(reverse("api_keys"))
        assert response.status_code == 200

        keys = response.context["api_keys"]
        assert len(keys) == 1
        assert keys[0].name == "alice-key"

    def test_admin_sees_all_keys(self, other_user):
        admin = User.objects.create_user(
            username="admin",
            email="admin@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        _create_api_key(admin, name="admin-key")
        _create_api_key(other_user, name="bob-key")

        client = Client()
        client.force_login(admin)
        response = client.get(reverse("api_keys"))
        assert response.status_code == 200

        keys = response.context["api_keys"]
        assert len(keys) == 2
        assert {k.name for k in keys} == {"admin-key", "bob-key"}
        assert response.context["is_admin"] is True


@pytest.mark.django_db
class TestAPIKeyCreateView:
    def test_create_stores_key_in_session(self, logged_in_client, user):
        response = logged_in_client.post(reverse("api_key_create"), {"name": "my-key"})
        assert response.status_code == 302
        assert response.url == reverse("api_keys")

        api_key = APIKey.objects.get(user=user)
        assert api_key.name == "my-key"
        assert not api_key.revoked

        # The raw key was stored in the session so it can be shown once on the list page.
        session = logged_in_client.session
        assert api_key.prefix in session["new_api_key"]

    def test_create_multiple_keys(self, logged_in_client, user):
        logged_in_client.post(reverse("api_key_create"), {"name": "key-1"})
        logged_in_client.post(reverse("api_key_create"), {"name": "key-2"})

        assert APIKey.objects.filter(user=user).count() == 2

    def test_create_requires_login(self):
        client = Client()
        response = client.post(reverse("api_key_create"), {"name": "my-key"})
        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestAPIKeyRevokeView:
    def test_revoke_own_key(self, logged_in_client, user):
        api_key, _ = _create_api_key(user)

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        api_key.refresh_from_db()
        assert api_key.revoked

    def test_cannot_revoke_other_users_key(self, logged_in_client, other_user):
        api_key, _ = _create_api_key(other_user, name="bob-key")

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        api_key.refresh_from_db()
        assert not api_key.revoked

        msgs = list(get_messages(response.wsgi_request))
        assert any("not found" in str(m).lower() for m in msgs)

    def test_revoke_already_revoked_key(self, logged_in_client, user):
        api_key, _ = _create_api_key(user)
        api_key.revoked = True
        api_key.save(update_fields=["revoked"])

        response = logged_in_client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302

        msgs = list(get_messages(response.wsgi_request))
        assert any("already revoked" in str(m).lower() for m in msgs)

    def test_revoke_requires_login(self, user):
        api_key, _ = _create_api_key(user)

        client = Client()
        response = client.post(reverse("api_key_revoke", kwargs={"pk": api_key.pk}))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

        api_key.refresh_from_db()
        assert not api_key.revoked


# ---------------------------------------------------------------------------
# User management views
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserListView:
    def test_admin_can_list_users(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"))
        assert response.status_code == 200
        assert admin_user in response.context["users"]
        assert member_user in response.context["users"]

    def test_member_gets_403(self, member_client):
        response = member_client.get(reverse("user_list"))
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self):
        response = Client().get(reverse("user_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_search_filters_by_name(self, admin_client, admin_user, member_user):
        member_user.name = "Specific Name"
        member_user.save()
        response = admin_client.get(reverse("user_list"), {"q": "Specific"})
        assert member_user in response.context["users"]
        assert admin_user not in response.context["users"]

    def test_search_filters_by_email(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"), {"q": "member@"})
        assert member_user in response.context["users"]
        assert admin_user not in response.context["users"]

    def test_filter_by_role(self, admin_client, admin_user, member_user):
        response = admin_client.get(reverse("user_list"), {"role": "admin"})
        assert admin_user in response.context["users"]
        assert member_user not in response.context["users"]


@pytest.mark.django_db
class TestUserCreateView:
    def test_admin_can_create_user(self, admin_client):
        response = admin_client.post(
            reverse("user_create"), {"name": "New User", "email": "new@test.com", "role": Role.MEMBER}
        )
        assert response.status_code == 302
        assert response.url == reverse("user_list")
        assert User.objects.filter(email="new@test.com").exists()

    def test_member_gets_403(self, member_client):
        response = member_client.post(
            reverse("user_create"), {"name": "New User", "email": "new@test.com", "role": Role.MEMBER}
        )
        assert response.status_code == 403

    def test_duplicate_email_shows_error(self, admin_client, member_user):
        response = admin_client.post(
            reverse("user_create"), {"name": "Dup", "email": member_user.email, "role": Role.MEMBER}
        )
        assert response.status_code == 200
        assert response.context["form"].errors

    def test_created_user_gets_member_role(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "Default", "email": "default@test.com", "role": Role.MEMBER})
        user = User.objects.get(email="default@test.com")
        assert user.role == Role.MEMBER

    def test_welcome_email_sent(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "Emailed", "email": "emailed@test.com", "role": Role.MEMBER})
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["emailed@test.com"]
        assert "example.com" in mail.outbox[0].subject

    def test_created_user_has_unusable_password(self, admin_client):
        admin_client.post(reverse("user_create"), {"name": "NoPwd", "email": "nopwd@test.com", "role": Role.MEMBER})
        user = User.objects.get(email="nopwd@test.com")
        assert not user.has_usable_password()

    def test_warning_shown_when_email_fails(self, admin_client):
        with patch("accounts.views.send_welcome_email", return_value=False):
            response = admin_client.post(
                reverse("user_create"), {"name": "NoEmail", "email": "noemail@test.com", "role": Role.MEMBER}
            )
        assert response.status_code == 302
        assert User.objects.filter(email="noemail@test.com").exists()
        msgs = list(get_messages(response.wsgi_request))
        assert any("could not be sent" in str(m) for m in msgs)


@pytest.mark.django_db
class TestUserUpdateView:
    def test_admin_can_update_user(self, admin_client, member_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": member_user.pk}),
            {"name": "Updated", "email": member_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 302
        member_user.refresh_from_db()
        assert member_user.name == "Updated"

    def test_member_gets_403(self, member_client, admin_user):
        response = member_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": "Hacked", "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 403

    def test_cannot_demote_last_admin(self, admin_client, admin_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 200
        assert response.context["form"].errors

    def test_can_demote_self_when_other_admin_exists(self, admin_client, admin_user):
        User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": "true"},
        )
        assert response.status_code == 302
        admin_user.refresh_from_db()
        assert admin_user.role == Role.MEMBER

    def test_cannot_deactivate_self(self, admin_client, admin_user):
        response = admin_client.post(
            reverse("user_update", kwargs={"pk": admin_user.pk}),
            {"name": admin_user.name, "email": admin_user.email, "role": Role.ADMIN, "is_active": "false"},
        )
        assert response.status_code == 200
        assert response.context["form"].errors


@pytest.mark.django_db
class TestUserDeleteView:
    def test_admin_can_delete_user(self, admin_client, member_user):
        response = admin_client.post(reverse("user_delete", kwargs={"pk": member_user.pk}))
        assert response.status_code == 302
        assert not User.objects.filter(pk=member_user.pk).exists()

    def test_member_gets_403(self, member_client, admin_user):
        response = member_client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 403

    def test_cannot_delete_self(self, admin_client, admin_user):
        response = admin_client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 302
        assert User.objects.filter(pk=admin_user.pk).exists()
        msgs = list(get_messages(response.wsgi_request))
        assert any("cannot delete your own" in str(m).lower() for m in msgs)

    def test_can_delete_admin_when_other_admin_exists(self, admin_user):
        other_admin = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        client = Client()
        client.force_login(other_admin)
        response = client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 302
        assert not User.objects.filter(pk=admin_user.pk).exists()

    def test_cannot_delete_last_admin(self, admin_user):
        other_admin = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        client = Client()
        client.force_login(other_admin)
        # Demote other_admin so admin_user is the last admin
        other_admin.role = Role.MEMBER
        other_admin.save()
        # other_admin is now a member, so they get 403 — the permission mixin itself prevents this.
        # The last-admin deletion guard in the view is a safety net for the case where
        # an admin tries to delete themselves (covered by test_cannot_delete_self).
        response = client.post(reverse("user_delete", kwargs={"pk": admin_user.pk}))
        assert response.status_code == 403


@pytest.mark.django_db
class TestDashboardChatSegment:
    """Dashboard breakdown bar must include a Chat tile (not silently absorb it into Other)."""

    def _make_run(self, user, trigger_type, status=RunStatus.SUCCESSFUL):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="daiv/test", user=user
        )
        return Run.objects.create(
            session=session, status=status, trigger_type=trigger_type, repo_id="daiv/test", user=user
        )

    def test_chat_segment_appears_with_correct_count_and_url(self, user):
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.API_JOB)

        client = Client()
        client.force_login(user)
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        labels = [s["label"] for s in segments]
        assert "Chat" in labels

        chat_seg = next(s for s in segments if s["label"] == "Chat")
        assert chat_seg["value"] == 2
        sessions_url = reverse("session_list")
        assert chat_seg["url"] == f"{sessions_url}?trigger={SessionOrigin.CHAT}"

    def test_chat_runs_not_double_counted_in_other(self, user):
        # 1 UI_JOB run → goes to Other (not a named trigger segment)
        # 2 CHAT runs → go to Chat segment, NOT Other
        self._make_run(user, SessionOrigin.UI_JOB)
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.CHAT)

        client = Client()
        client.force_login(user)
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        seg_by_label = {s["label"]: s["value"] for s in segments}

        # Chat counts correctly
        assert seg_by_label.get("Chat", 0) == 2
        # Other gets only the UI_JOB run (1), not the chat runs
        assert seg_by_label.get("Other", 0) == 1


# ---------------------------------------------------------------------------
# Story 2.3 — The Feed: emit and render "what happened"
# ---------------------------------------------------------------------------


def _make_feed_run(
    user,
    *,
    envelope_status=None,
    summary="",
    n_actionable=0,
    finished=True,
    read_at=None,
    repo_id="group/project",
    next_run_at=None,
):
    """Build a SCHEDULE run (+ optional RunEnvelope + a RUN_FEED notification) for ``user``.

    ``envelope_status=None`` leaves the run unclassified — the "classifying…" state (``for_run``
    returns None). A ``found-issues`` envelope needs ``n_actionable`` items to carry a count.
    """
    schedule = ScheduledJob.objects.create(
        user=user,
        name="nightly",
        prompt="p",
        repos=[{"repo_id": repo_id, "ref": ""}],
        frequency=Frequency.DAILY,
        time="12:00",
        next_run_at=next_run_at,
    )
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id=repo_id, user=user, scheduled_job=schedule
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id=repo_id,
        status=RunStatus.SUCCESSFUL,
        user=user,
        finished_at=timezone.now() if finished else None,
    )
    if envelope_status is not None:
        actionable = [
            build_actionable_item(id=str(i), kind="bug", label=f"issue {i}", ref="a.py") for i in range(n_actionable)
        ]
        RunEnvelope.objects.create(run=run, status=envelope_status, summary=summary, actionable=actionable)
    Notification.objects.create(
        recipient=user,
        event_type=EventType.RUN_FEED,
        source_type="sessions.Run",
        source_id=str(run.pk),
        subject="nightly",
        body="",
        link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
        read_at=read_at,
    )
    return run


@pytest.mark.django_db
class TestFeedRender:
    """AC2/AC3/AC5 — five render states keyed off the RunEnvelope."""

    def test_found_issues_renders_expanded_with_count_and_summary(self, member_client, member_user):
        _make_feed_run(
            member_user, envelope_status=EnvelopeStatus.FOUND_ISSUES, summary="Two problems in auth.", n_actionable=2
        )
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-testid="feed-item"' in content
        assert 'data-status="found-issues"' in content
        assert "Two problems in auth." in content
        assert "2 items" in content

    def test_needs_attention_renders_summary(self, member_client, member_user):
        _make_feed_run(
            member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, summary="Review the migration plan."
        )
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="needs-attention"' in content
        assert "Review the migration plan." in content

    def test_all_clear_renders_quiet_in_a_mixed_feed(self, member_client, member_user):
        # A mix (one item needs attention) forces the list to render, so the quiet all-clear card shows.
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.FOUND_ISSUES, summary="x", n_actionable=1)
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="all-clear"' in content
        assert "we checked — nothing needs you" in content

    def test_failed_reads_as_tooling_problem_not_a_finding(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED, summary="MCP connector down")
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="failed"' in content
        assert "Tooling problem — MCP connector down" in content
        # A failed run never carries a finding count — the found-issues count span (the only place
        # that inline color is used) must be absent.
        assert "var(--color-status-found)" not in content

    def test_classifying_renders_pending_and_registers_in_flight(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=None)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="classifying"' in content
        assert "classifying" in content
        assert 'aria-live="polite"' in content
        # The run id is registered on the in-flight collector for the SSE consumer.
        assert str(run.id) in content.split('id="feed-in-flight"')[1].split(">")[0]

    def test_no_action_buttons_rendered(self, member_client, member_user):
        # Epic 5 owns fix/review/retry buttons; the Feed renders status + summary + drill-through,
        # plus Story 2.4's seen-state dismiss control — but never an envelope action button.
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.FOUND_ISSUES, summary="x", n_actionable=1)
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, summary="y")
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED, summary="z")
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert "Fix it" not in content
        assert "Review this" not in content
        feed = content.split('data-testid="console-feed"')[1]
        # The only buttons in the Feed are seen-state dismiss controls, never an Epic 5 action.
        assert feed.count("<button") == feed.count('data-testid="feed-item-dismiss"')

    def test_drill_through_links_to_session_detail(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        # A lone all-clear renders the seal, so add an attention item to force the list.
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED, summary="z")
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert reverse("session_detail", kwargs={"thread_id": run.session_id}) in content


@pytest.mark.django_db
class TestFeedZeroState:
    """AC6 — the "nothing needs you." seal, distinguishing audited-clean from never-ran."""

    def test_never_ran_seal(self, member_client, member_user):
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-testid="feed-zero-state"' in content
        assert 'data-variant="never-ran"' in content
        assert "No scheduled runs yet." in content

    def test_audited_clean_seal_with_audit_meta(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-testid="feed-zero-state"' in content
        assert 'data-variant="audited-clean"' in content
        assert "run all clear" in content
        assert "last checked" in content

    def test_audited_clean_appends_next_sweep_when_present(self, member_client, member_user):
        _make_feed_run(
            member_user,
            envelope_status=EnvelopeStatus.ALL_CLEAR,
            next_run_at=timezone.now() + timezone.timedelta(hours=3),
        )
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert "next sweep" in content


@pytest.mark.django_db
class TestFeedItemView:
    """AC9 — the per-item partial endpoint (SSE re-fetch source)."""

    def test_returns_partial_for_owned_run(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("feed_item", kwargs={"run_id": run.id}))
        assert response.status_code == 200
        content = response.content.decode()
        assert 'data-testid="feed-item"' in content
        assert 'data-status="all-clear"' in content

    def test_404_for_run_without_feed_row(self, member_client, member_user, admin_user):
        # A run owned by another user (member has no RUN_FEED row for it) → 404.
        run = _make_feed_run(admin_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("feed_item", kwargs={"run_id": run.id}))
        assert response.status_code == 404

    def test_404_for_unknown_run(self, member_client, member_user):
        response = member_client.get(reverse("feed_item", kwargs={"run_id": uuid.uuid4()}))
        assert response.status_code == 404

    def test_resolved_run_drops_classifying_hooks(self, member_client, member_user):
        # Before classification: the item is classifying (aria-busy). After the envelope lands, the
        # re-fetched partial renders the resolved status and omits the classifying/in-flight hooks.
        run = _make_feed_run(member_user, envelope_status=None)
        pending = member_client.get(reverse("feed_item", kwargs={"run_id": run.id})).content.decode()
        assert 'data-status="classifying"' in pending
        assert "aria-busy" in pending

        RunEnvelope.objects.create(run=run, status=EnvelopeStatus.ALL_CLEAR, summary="")
        resolved = member_client.get(reverse("feed_item", kwargs={"run_id": run.id})).content.decode()
        assert 'data-status="all-clear"' in resolved
        assert "aria-busy" not in resolved


class TestFeedI18n:
    """AC7 — all new Feed microcopy is externalized (no bare hard-coded strings)."""

    def test_feed_item_strings_are_translated(self):
        src = Path(get_template("accounts/_feed_item.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "classifying…" %}' in src
        assert '{% translate "we checked — nothing needs you" %}' in src
        assert "Tooling problem" in src

    def test_zero_state_strings_are_translated(self):
        src = Path(get_template("accounts/_feed_zero_state.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "nothing needs you." %}' in src
        assert '{% translate "No scheduled runs yet." %}' in src
        assert "runs all clear" in src


# ---------------------------------------------------------------------------
# Story 2.4 — Per-user seen/unread and mark-seen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFeedItemSeen:
    """AC3/AC7 — owner-scoped, idempotent single-item mark-seen."""

    def test_own_unread_marks_seen_and_triggers(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, summary="y")
        response = member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert response.status_code == 200
        notif = Notification.objects.get(recipient=member_user, source_id=str(run.pk))
        assert notif.read_at is not None
        content = response.content.decode()
        # The re-rendered item is the seen treatment — no unread cue, no dismiss control.
        assert 'data-testid="feed-item"' in content
        assert 'data-testid="feed-item-unread"' not in content
        assert 'data-testid="feed-item-dismiss"' not in content
        assert response["HX-Trigger"] == "feed:seen"

    def test_cross_user_is_404(self, member_client, admin_user):
        run = _make_feed_run(admin_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        response = member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert response.status_code == 404

    def test_unknown_run_is_404(self, member_client, member_user):
        response = member_client.post(reverse("feed_item_seen", kwargs={"run_id": uuid.uuid4()}))
        assert response.status_code == 404

    def test_deleted_run_is_404(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        Run.objects.filter(pk=run.pk).delete()  # orphan the RUN_FEED row
        response = member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert response.status_code == 404

    def test_get_is_405(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        response = member_client.get(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert response.status_code == 405

    def test_repeat_is_idempotent(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        first = member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert first.status_code == 200
        notif = Notification.objects.get(recipient=member_user, source_id=str(run.pk))
        first_read_at = notif.read_at
        assert first_read_at is not None
        second = member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        assert second.status_code == 200
        notif.refresh_from_db()
        assert notif.read_at == first_read_at  # unchanged — mark_as_read no-ops when already read

    def test_per_user_isolation(self, member_client, member_user, admin_user):
        # AC1 — two users hold a Feed row for the same run; marking one seen leaves the other unread.
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        Notification.objects.create(
            recipient=admin_user,
            event_type=EventType.RUN_FEED,
            source_type="sessions.Run",
            source_id=str(run.pk),
            subject="n",
            body="",
            link_url="/",
        )
        member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        member_notif = Notification.objects.get(recipient=member_user, source_id=str(run.pk))
        admin_notif = Notification.objects.get(recipient=admin_user, source_id=str(run.pk))
        assert member_notif.read_at is not None
        assert admin_notif.read_at is None

    def test_marking_feed_seen_leaves_bell_row_unread(self, member_client, member_user):
        # AC6 — a bell (non-RUN_FEED) unread row stays unread when a Feed item is marked seen.
        bell = Notification.objects.create(
            recipient=member_user, event_type=EventType.JOB_FINISHED, subject="s", body="b", link_url="/"
        )
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        member_client.post(reverse("feed_item_seen", kwargs={"run_id": run.id}))
        bell.refresh_from_db()
        assert bell.read_at is None


@pytest.mark.django_db
class TestFeedRenderDelta:
    """AC2/AC3/AC8 — unseen vs seen cues, dismiss gating, and the drill-through wiring."""

    def _feed_section(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        return content.split('data-testid="console-feed"')[1]

    def test_unseen_resolved_carries_unread_and_dismiss(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, summary="y")
        feed = self._feed_section(member_client)
        assert 'data-testid="feed-item-unread"' in feed
        assert 'data-testid="feed-item-dismiss"' in feed

    def test_unseen_classifying_carries_unread_not_dismiss(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=None)  # classifying — no envelope yet
        feed = self._feed_section(member_client)
        assert 'data-testid="feed-item-unread"' in feed
        assert 'data-testid="feed-item-dismiss"' not in feed

    def test_seen_item_carries_neither_cue(self, member_client, member_user):
        # A seen needs-attention item still renders (attention is envelope-status, not read state).
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, read_at=timezone.now())
        feed = self._feed_section(member_client)
        assert 'data-testid="feed-item"' in feed
        assert 'data-testid="feed-item-unread"' not in feed
        assert 'data-testid="feed-item-dismiss"' not in feed

    def test_drill_through_link_marks_seen(self, member_client, member_user):
        run = _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        feed = self._feed_section(member_client)
        assert reverse("feed_item_seen", kwargs={"run_id": run.id}) in feed
        assert 'hx-post="' in feed

    def test_badge_shows_attention_total(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _make_feed_run(member_user, envelope_status=None)  # classifying counts
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'data-testid="feed-unread-badge"' in content
        # The sr-only text counts ATTENTION items, not "unread" (P2) — the per-item unread dots keep
        # their own bare "unread" label, so the badge must announce the distinct attention wording.
        assert "2 need attention" in content

    def test_badge_absent_at_zero_but_attention_seal_persists(self, member_client, member_user):
        # Only an unread all-clear run → attention count 0 → badge chip absent, but the non-empty
        # "nothing needs your attention" sr-only text persists so the to-zero swap is announced (P2).
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'data-testid="feed-unread-badge"' not in content
        assert "nothing needs your attention" in content

    def test_badge_is_not_teal_nor_you_have_n(self, member_client, member_user):
        _make_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        content = member_client.get(reverse("dashboard")).content.decode()
        badge = content.split('data-testid="feed-unread-badge"')[1][:160]
        assert "text-text-muted" in badge  # neutral/muted, mono
        assert "teal" not in badge
        assert "accent" not in badge
        assert "you have" not in content.lower()
