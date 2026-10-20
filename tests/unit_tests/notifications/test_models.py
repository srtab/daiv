import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

import pytest
from notifications.choices import DeliveryStatus, EnvelopeStatus, EventType
from notifications.models import Notification, NotificationDelivery, RunEnvelope, UserChannelBinding
from sessions.models import Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.notifications.conftest import actionable_item


@pytest.mark.django_db
class TestNotification:
    def test_create_minimal(self, member_user):
        n = Notification.objects.create(
            recipient=member_user,
            event_type="schedule.finished",
            subject="Hello",
            body="Body",
            link_url="/dashboard/activity/123/",
        )
        assert n.id is not None
        assert n.read_at is None
        assert n.created is not None
        assert n.context == {}

    def test_context_serializes_lazy_proxy_and_datetime(self, member_user):
        """context uses DjangoJSONEncoder, so payload builders can store gettext_lazy
        proxies and datetimes directly. Without it the save raises TypeError and the
        notification is silently lost (the batch-rollup regression this guards against)."""
        from django.utils import timezone
        from django.utils.translation import gettext_lazy as _

        n = Notification.objects.create(
            recipient=member_user,
            event_type="job.finished",
            subject="s",
            body="b",
            context={"status_label": _("Successful"), "finished_at": timezone.now()},
        )
        n.refresh_from_db()
        # Lazy proxy is forced to its translated string; datetime becomes an ISO string.
        assert n.context["status_label"] == "Successful"
        assert isinstance(n.context["finished_at"], str)

    def test_mark_as_read(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        assert n.read_at is None
        n.mark_as_read()
        n.refresh_from_db()
        assert n.read_at is not None

    def test_mark_as_read_is_idempotent(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        n.mark_as_read()
        original = n.read_at
        n.mark_as_read()
        n.refresh_from_db()
        assert n.read_at == original

    def test_mark_all_read_for_returns_count_and_preserves_already_read(self, member_user):
        unread = [
            Notification.objects.create(recipient=member_user, event_type="e", subject=f"u{i}", body="b", link_url="/")
            for i in range(3)
        ]
        already_read = Notification.objects.create(
            recipient=member_user, event_type="e", subject="r", body="b", link_url="/"
        )
        already_read.mark_as_read()
        original_read_at = Notification.objects.get(pk=already_read.pk).read_at

        updated = Notification.mark_all_read_for(member_user)

        assert updated == len(unread)
        assert Notification.objects.filter(recipient=member_user, read_at__isnull=True).count() == 0
        # Already-read rows must keep their original read_at — the helper must scope to unread only.
        assert Notification.objects.get(pk=already_read.pk).read_at == original_read_at

    def test_mark_all_read_for_leaves_other_users_unread(self, member_user, admin_user):
        other = Notification.objects.create(recipient=admin_user, event_type="e", subject="o", body="b", link_url="/")
        Notification.objects.create(recipient=member_user, event_type="e", subject="m", body="b", link_url="/")

        Notification.mark_all_read_for(member_user)

        other.refresh_from_db()
        assert other.read_at is None

    def test_mark_all_read_for_bumps_modified(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        original_modified = n.modified
        Notification.mark_all_read_for(member_user)
        n.refresh_from_db()
        assert n.modified > original_modified


@pytest.mark.django_db
class TestNotificationDelivery:
    def test_defaults(self, member_user):
        n = Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
        )
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 0
        assert d.delivered_at is None

    def test_unique_per_channel_per_notification(self, member_user):
        n = Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
        )
        NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        with pytest.raises(IntegrityError):
            NotificationDelivery.objects.create(notification=n, channel_type="email", address="other@b.com")

    def test_mark_sent(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_sent()
        d.refresh_from_db()
        assert d.status == DeliveryStatus.SENT
        assert d.delivered_at is not None
        assert d.error_message == ""

    def test_mark_failed(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_failed("connection refused")
        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.error_message == "connection refused"

    def test_mark_skipped(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_skipped("no binding")
        d.refresh_from_db()
        assert d.status == DeliveryStatus.SKIPPED
        assert d.error_message == "no binding"

    def test_sent_without_delivered_at_violates_constraint(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        with pytest.raises(IntegrityError):
            NotificationDelivery.objects.filter(pk=d.pk).update(status=DeliveryStatus.SENT, delivered_at=None)


@pytest.mark.django_db
class TestUserChannelBinding:
    def test_unique_user_channel_address(self, member_user):
        UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")
        with pytest.raises(IntegrityError):
            UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")

    def test_verified_without_timestamp_violates_constraint(self, member_user):
        with pytest.raises(IntegrityError):
            UserChannelBinding.objects.create(
                user=member_user, channel_type="slack", address="u-1", is_verified=True, verified_at=None
            )


@pytest.mark.django_db
def test_per_run_event_dedup_constraint_rejects_duplicate(member_user):
    kwargs = dict(  # noqa: C408
        recipient=member_user,
        event_type=EventType.JOB_FINISHED,
        source_type="sessions.Run",
        source_id="run-123",
        subject="s",
        body="b",
        link_url="/",
    )
    Notification.objects.create(**kwargs)
    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(**kwargs)


@pytest.mark.django_db
def test_schedule_finished_event_dedup_constraint_rejects_duplicate(member_user):
    kwargs = dict(  # noqa: C408
        recipient=member_user,
        event_type=EventType.SCHEDULE_FINISHED,
        source_type="sessions.Run",
        source_id="run-xyz",
        subject="s",
        body="b",
        link_url="/",
    )
    Notification.objects.create(**kwargs)
    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(**kwargs)


def _mk_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.SCHEDULE, "repo_id": "group/repo"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _mk_run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.SCHEDULE, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


def _mk_envelope(run: Run, **overrides) -> RunEnvelope:
    defaults = {"status": EnvelopeStatus.ALL_CLEAR}
    defaults.update(overrides)
    return RunEnvelope.objects.create(run=run, **defaults)


@pytest.mark.django_db
class TestRunEnvelope:
    def test_run_envelope_status_constraint_literals_match_enum(self):
        """The ``run_envelope_status_valid`` CHECK references ``EnvelopeStatus.values`` so it cannot
        drift; assert that intent-documenting equivalence (mirrors the ``Run`` drift guard)."""
        constraint = next(c for c in RunEnvelope._meta.constraints if c.name == "run_envelope_status_valid")
        conditions = dict(constraint.condition.children)
        assert set(conditions["status__in"]) == set(EnvelopeStatus.values)

    def test_valid_all_clear_envelope_persists(self):
        env = _mk_envelope(_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR)
        env.refresh_from_db()
        assert env.status == EnvelopeStatus.ALL_CLEAR
        assert env.count == 0
        assert env.summary == ""
        assert env.actionable == []

    def test_bogus_status_create_rejected_by_check_constraint(self):
        run = _mk_run(_mk_session())
        with pytest.raises(IntegrityError):
            RunEnvelope.objects.create(run=run, status="bogus")

    def test_bogus_status_update_rejected_by_check_constraint(self):
        """``.update()`` bypasses field validation, so the DB CHECK is the real guarantee."""
        env = _mk_envelope(_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR)
        with pytest.raises(IntegrityError):
            RunEnvelope.objects.filter(pk=env.pk).update(status="bogus")

    def test_duplicate_envelope_for_same_run_rejected(self):
        run = _mk_run(_mk_session())
        _mk_envelope(run, status=EnvelopeStatus.ALL_CLEAR)
        with pytest.raises(IntegrityError):
            _mk_envelope(run, status=EnvelopeStatus.NEEDS_ATTENTION)

    def test_full_clean_rejects_malformed_actionable(self):
        env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.NEEDS_ATTENTION, actionable=[{"id": "x"}])
        with pytest.raises(ValidationError):
            env.full_clean()

    def test_needs_input_passes_the_status_check_constraint(self):
        env = RunEnvelope.objects.create(run=_mk_run(_mk_session()), status=EnvelopeStatus.NEEDS_INPUT)
        assert env.status == "needs-input"

    def test_clean_rejects_found_issues_with_empty_actionable(self):
        env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.FOUND_ISSUES, actionable=[])
        with pytest.raises(ValidationError):
            env.full_clean()

    def test_clean_accepts_found_issues_with_actionable(self):
        env = RunEnvelope(
            run=_mk_run(_mk_session()), status=EnvelopeStatus.FOUND_ISSUES, actionable=[actionable_item()]
        )
        env.full_clean()  # no raise

    def test_ordering_has_deterministic_tiebreaker(self):
        assert RunEnvelope._meta.ordering == ["-created_at", "-id"]

    def test_clean_rejects_non_found_issues_carrying_actionable(self):
        """The reverse coherence direction: only a found-issues envelope may carry actionable items."""
        env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR, actionable=[actionable_item()])
        with pytest.raises(ValidationError):
            env.full_clean()

    def test_count_is_derived_from_actionable_on_save(self):
        """``count`` mirrors ``len(actionable)`` regardless of any value a caller passes to ``create``."""
        run = _mk_run(_mk_session())
        env = RunEnvelope.objects.create(
            run=run,
            status=EnvelopeStatus.FOUND_ISSUES,
            actionable=[actionable_item("a"), actionable_item("b")],
            count=99,
        )
        env.refresh_from_db()
        assert env.count == 2
