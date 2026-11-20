from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from django.utils import timezone

import pytest
from sessions.artifacts import ArtifactKind, content_types_for_kind
from sessions.filters import STATUS_CHOICES, ArtifactFilter, SessionFilter, resolve_scope
from sessions.models import (
    Run,
    RunArtifact,
    RunStatus,
    Session,
    SessionOrigin,
    SessionRepository,
    SpendRecord,
    SpendTask,
)

from accounts.models import User
from schedules.models import Frequency, ScheduledJob
from tests.unit_tests.sessions.conftest import make_artifact


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
    )


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _qs():
    """Base queryset with the annotation required by filter_status."""
    return Session.objects.with_latest_status()


def test_status_choices_put_what_waits_on_the_developer_first():
    assert [value for value, _label in STATUS_CHOICES] == [
        RunStatus.WAITING_INPUT,
        RunStatus.FAILED,
        RunStatus.RUNNING,
        RunStatus.QUEUED,
        RunStatus.READY,
        RunStatus.SUCCESSFUL,
    ]


def test_status_choices_cover_every_run_status():
    assert sorted(value for value, _label in STATUS_CHOICES) == sorted(RunStatus.values)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, "mine"),
        ({"q": "fix", "status": "FAILED"}, "mine"),
        ({"repo": "group/project"}, "all"),
        ({"mr": "42"}, "all"),
        ({"schedule": "7"}, "all"),
        ({"batch": str(uuid.UUID(int=1))}, "all"),
        ({"repo": ""}, "mine"),
        ({"repo": "group/project", "scope": "mine"}, "mine"),
        ({"scope": "all"}, "all"),
        ({"scope": "everyone"}, "mine"),
        ({"scope": "everyone", "repo": "group/project"}, "all"),
    ],
)
def test_resolve_scope(data, expected):
    assert resolve_scope(data) == expected


@pytest.mark.django_db
class TestSessionFilter:
    def test_no_params_returns_all(self, user):
        a = _create_session()
        b = _create_session()
        qs = SessionFilter({}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_trigger_filter(self, user):
        sched = _create_session(origin=SessionOrigin.SCHEDULE)
        webhook = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK)
        qs = SessionFilter({"trigger": SessionOrigin.SCHEDULE}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert sched.pk in pks
        assert webhook.pk not in pks

    def test_repo_filter(self, user):
        a = _create_session(repo_id="group/project")
        b = _create_session(repo_id="group/other")
        qs = SessionFilter({"repo": "group/project"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk not in pks

    def test_schedule_filter_invalid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"schedule": "not-a-number"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_schedule_filter_matches_fk(self, user):
        job = ScheduledJob.objects.create(
            user=user,
            name="nightly",
            prompt="x",
            repos=[{"repo_id": "group/project", "ref": ""}],
            frequency=Frequency.DAILY,
            time=time(3, 0),
        )
        match = _create_session(scheduled_job=job)
        other = _create_session()
        qs = SessionFilter({"schedule": str(job.pk)}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_date_from_filter(self, user):
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_from": "2025-06-01"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert recent.pk in pks
        assert old.pk not in pks

    def test_date_to_filter(self, user):
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_to": "2025-06-01"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert old.pk in pks
        assert recent.pk not in pks

    def test_date_range_combined(self, user):
        before = _create_session()
        Session.objects.filter(pk=before.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        inside = _create_session()
        Session.objects.filter(pk=inside.pk).update(last_active_at=datetime(2025, 6, 15, tzinfo=UTC))
        after = _create_session()
        Session.objects.filter(pk=after.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_from": "2025-01-01", "date_to": "2025-12-31"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert inside.pk in pks
        assert before.pk not in pks
        assert after.pk not in pks

    def test_ran_on_matches_a_run_that_day_not_the_last_activity(self, user):
        day = timezone.localdate() - timedelta(days=3)
        ran_that_day = _create_session()
        Run.objects.filter(pk=_create_run(ran_that_day).pk).update(
            created_at=timezone.make_aware(datetime.combine(day, time(12)))
        )
        other_day = _create_session()
        Run.objects.filter(pk=_create_run(other_day).pk).update(
            created_at=timezone.make_aware(datetime.combine(day - timedelta(days=1), time(12)))
        )

        pks = list(SessionFilter({"ran_on": day.isoformat()}, queryset=_qs()).qs.values_list("pk", flat=True))

        assert pks == [ran_that_day.pk]

    def test_invalid_date_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"date_from": "not-a-date"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_combined_filters(self, user):
        match = _create_session(origin=SessionOrigin.SCHEDULE, repo_id="group/project")
        _create_run(match, status=RunStatus.SUCCESSFUL)
        wrong_origin = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/project")
        _create_run(wrong_origin, status=RunStatus.SUCCESSFUL)
        wrong_repo = _create_session(origin=SessionOrigin.SCHEDULE, repo_id="group/other")
        _create_run(wrong_repo, status=RunStatus.SUCCESSFUL)

        qs = SessionFilter({"trigger": SessionOrigin.SCHEDULE, "repo": "group/project"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert wrong_origin.pk not in pks
        assert wrong_repo.pk not in pks

    # --- Brief-specified new tests ---

    def test_status_filters_on_latest_run(self, user):
        """?status=RUNNING matches a session whose LATEST run is RUNNING, and does not
        match a session whose latest run is SUCCESSFUL even if an older one was RUNNING."""
        # Session A: older RUNNING, newer SUCCESSFUL → should NOT match ?status=RUNNING
        session_a = _create_session()
        _create_run(session_a, status=RunStatus.RUNNING)
        _create_run(session_a, status=RunStatus.SUCCESSFUL)

        # Session B: only run is RUNNING → SHOULD match
        session_b = _create_session()
        _create_run(session_b, status=RunStatus.RUNNING)

        qs = SessionFilter({"status": RunStatus.RUNNING}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert session_b.pk in pks
        assert session_a.pk not in pks

    def test_trigger_filters_on_origin(self, user):
        """?trigger=issue_webhook returns webhook-origin sessions; ?trigger=chat returns chat sessions."""
        webhook_session = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK)
        chat_session = _create_session(origin=SessionOrigin.CHAT)
        other_session = _create_session(origin=SessionOrigin.SCHEDULE)

        webhook_qs = SessionFilter({"trigger": SessionOrigin.ISSUE_WEBHOOK}, queryset=_qs()).qs
        webhook_pks = list(webhook_qs.values_list("pk", flat=True))
        assert webhook_session.pk in webhook_pks
        assert chat_session.pk not in webhook_pks
        assert other_session.pk not in webhook_pks

        chat_qs = SessionFilter({"trigger": SessionOrigin.CHAT}, queryset=_qs()).qs
        chat_pks = list(chat_qs.values_list("pk", flat=True))
        assert chat_session.pk in chat_pks
        assert webhook_session.pk not in chat_pks

    def test_batch_filters_via_runs(self, user):
        """?batch=<uuid> returns sessions containing a run with that batch_id."""
        batch_id = uuid.uuid4()

        session_with_batch = _create_session()
        _create_run(session_with_batch, batch_id=batch_id)

        session_other_batch = _create_session()
        _create_run(session_other_batch, batch_id=uuid.uuid4())

        session_no_batch = _create_session()

        qs = SessionFilter({"batch": str(batch_id)}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert session_with_batch.pk in pks
        assert session_other_batch.pk not in pks
        assert session_no_batch.pk not in pks

    def test_mr_matches_sessions_scoped_to_the_merge_request(self, user):
        """?mr=<iid> collects every session that touched the request, whichever way it learned of it."""
        from_webhook = _create_session(merge_request_iid=42)

        from_run_backfill = _create_session()
        _create_run(from_run_backfill, merge_request_iid=42)

        other_mr = _create_session(merge_request_iid=43)
        no_mr = _create_session()

        pks = list(SessionFilter({"mr": "42"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert from_webhook.pk in pks
        assert from_run_backfill.pk in pks
        assert other_mr.pk not in pks
        assert no_mr.pk not in pks

    def test_mr_returns_a_session_once_despite_several_matching_runs(self, user):
        """The runs join would otherwise duplicate the row per matching run."""
        session = _create_session(merge_request_iid=42)
        _create_run(session, merge_request_iid=42)
        _create_run(session, merge_request_iid=42)

        pks = list(SessionFilter({"mr": "42"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert pks.count(session.pk) == 1

    def test_mr_is_scoped_to_the_repository_when_one_is_given(self, user):
        """IIDs are per-project, so !42 of another repo must not join the list."""
        here = _create_session(repo_id="group/project", merge_request_iid=42)
        elsewhere = _create_session(repo_id="other/project", merge_request_iid=42)

        pks = list(SessionFilter({"mr": "42", "repo": "group/project"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert here.pk in pks
        assert elsewhere.pk not in pks

    def test_mr_with_a_repository_matches_it_on_any_of_a_session_s_repositories(self, user):
        shared = _create_session(repo_id="group/main")
        SessionRepository.objects.create(session=shared, position=1, repo_id="group/project", merge_request_iid=42)

        pks = list(SessionFilter({"mr": "42", "repo": "group/project"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert pks == [shared.pk]

    def test_mr_filter_invalid_value_is_ignored(self, user):
        a = _create_session(merge_request_iid=42)
        f = SessionFilter({"mr": "not-a-number"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_invalid_status_drops_filter(self, user):
        """?status=bogus returns the unfiltered list (strict=False semantics)."""
        a = _create_session()
        b = _create_session()
        f = SessionFilter({"status": "bogus"}, queryset=_qs())
        assert not f.form.is_valid()
        # Invalid choice is dropped → no filter applied; all sessions returned.
        pks = list(f.qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_batch_filter_invalid_uuid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"batch": "not-a-uuid"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_q_matches_title(self, user):
        match = _create_session(title="Fix the crash", repo_id="group/project")
        other = _create_session(title="Add a feature", repo_id="group/project")
        qs = SessionFilter({"q": "crash"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_q_matches_repo_case_insensitive(self, user):
        match = _create_session(title="", repo_id="Group/Payments")
        other = _create_session(title="", repo_id="group/other")
        qs = SessionFilter({"q": "payments"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_q_empty_is_noop(self, user):
        a = _create_session()
        b = _create_session()
        pks = list(SessionFilter({"q": ""}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_range_7d_includes_recent_excludes_old(self, user):
        now = timezone.now()
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=now - timedelta(days=2))
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=now - timedelta(days=20))
        pks = list(SessionFilter({"range": "7d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert recent.pk in pks
        assert old.pk not in pks

    def test_range_today_uses_local_midnight(self, user):
        now = timezone.now()
        today = _create_session()
        Session.objects.filter(pk=today.pk).update(last_active_at=now)
        yesterday = _create_session()
        Session.objects.filter(pk=yesterday.pk).update(last_active_at=now - timedelta(days=1, hours=1))
        pks = list(SessionFilter({"range": "today"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert today.pk in pks
        assert yesterday.pk not in pks

    def test_range_invalid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"range": "bogus"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_q_matches_both_title_and_repo_returns_row_once(self, user):
        # filter_q ORs two columns of the SAME row (no join), so a term hitting both
        # title and repo_id must not duplicate the row.
        match = _create_session(title="payments dashboard", repo_id="group/payments")
        pks = list(SessionFilter({"q": "payments"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert pks.count(match.pk) == 1

    def test_range_2d_and_30d_windows(self, user):
        now = timezone.now()
        d1 = _create_session()
        Session.objects.filter(pk=d1.pk).update(last_active_at=now - timedelta(days=1))
        d3 = _create_session()
        Session.objects.filter(pk=d3.pk).update(last_active_at=now - timedelta(days=3))
        d20 = _create_session()
        Session.objects.filter(pk=d20.pk).update(last_active_at=now - timedelta(days=20))
        d40 = _create_session()
        Session.objects.filter(pk=d40.pk).update(last_active_at=now - timedelta(days=40))

        pks_2d = set(SessionFilter({"range": "2d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert d1.pk in pks_2d
        assert d3.pk not in pks_2d
        assert d20.pk not in pks_2d

        pks_30d = set(SessionFilter({"range": "30d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert {d1.pk, d3.pk, d20.pk} <= pks_30d
        assert d40.pk not in pks_30d

    def test_range_boundary_is_inclusive(self, user):
        # last_active_at__gte makes the window edge inclusive. Use ~1h margins around the
        # 7-day edge so the assertion can't flake on the sub-second gap between the test's
        # ``now`` and filter_range's own ``timezone.now()``.
        now = timezone.now()
        just_in = _create_session()
        Session.objects.filter(pk=just_in.pk).update(last_active_at=now - timedelta(days=6, hours=23))
        just_out = _create_session()
        Session.objects.filter(pk=just_out.pk).update(last_active_at=now - timedelta(days=7, hours=1))
        pks = set(SessionFilter({"range": "7d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert just_in.pk in pks
        assert just_out.pk not in pks


@pytest.mark.django_db
class TestArtifactFilter:
    def test_q_matches_title_case_insensitive(self, user):
        session = _create_session()
        run = _create_run(session)
        match = make_artifact(run, filename="report.md", title="Findings")
        other = make_artifact(run, filename="notes.txt", title="Something else")
        pks = list(ArtifactFilter({"q": "FIND"}, queryset=RunArtifact.objects.all()).qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_q_matches_filename_case_insensitive(self, user):
        session = _create_session()
        run = _create_run(session)
        match = make_artifact(run, filename="Audit-Report.html", title="Something")
        other = make_artifact(run, filename="data.csv", title="Different")
        pks = list(ArtifactFilter({"q": "audit"}, queryset=RunArtifact.objects.all()).qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_kind_known_matches_only_that_kind(self, user):
        session = _create_session()
        run = _create_run(session)
        image = make_artifact(run, filename="chart.png")
        text = make_artifact(run, filename="data.csv")
        pks = list(
            ArtifactFilter({"kind": ArtifactKind.IMAGE}, queryset=RunArtifact.objects.all()).qs.values_list(
                "pk", flat=True
            )
        )
        assert image.pk in pks
        assert text.pk not in pks

    @pytest.mark.parametrize("known_kind", [kind for kind in ArtifactKind if kind != ArtifactKind.OTHER])
    def test_kind_other_excludes_every_known_kind(self, user, known_kind):
        session = _create_session()
        run = _create_run(session)
        unrecognized = make_artifact(run, filename="report.pdf")
        known = []
        for content_type in content_types_for_kind(known_kind):
            artifact = make_artifact(run)
            RunArtifact.objects.filter(pk=artifact.pk).update(content_type=content_type)
            known.append(artifact.pk)
        pks = list(
            ArtifactFilter({"kind": ArtifactKind.OTHER}, queryset=RunArtifact.objects.all()).qs.values_list(
                "pk", flat=True
            )
        )
        assert unrecognized.pk in pks
        assert known
        assert not set(known) & set(pks)

    def test_repo_filter(self, user):
        here = make_artifact(_create_run(_create_session(repo_id="group/project")))
        elsewhere = make_artifact(_create_run(_create_session(repo_id="group/other")))
        pks = list(
            ArtifactFilter({"repo": "group/project"}, queryset=RunArtifact.objects.all()).qs.values_list(
                "pk", flat=True
            )
        )
        assert here.pk in pks
        assert elsewhere.pk not in pks


def _spend(session: Session, *, days_ago: int = 0, **fields) -> SpendRecord:
    fields.setdefault("trigger_type", SessionOrigin.CHAT)
    return SpendRecord.objects.create(
        session=session,
        occurred_at=timezone.now() - timedelta(days=days_ago),
        model=fields.pop("model", "a"),
        repo_id=fields.pop("repo_id", session.repo_id),
        **fields,
    )


def _spend_pks(params: dict) -> set[str]:
    return set(SessionFilter(params, queryset=_qs()).qs.values_list("pk", flat=True))


@pytest.mark.django_db
class TestSpendFilters:
    def test_every_spend_param_must_match_one_record(self, user):
        session = _create_session()
        _spend(session, model="a", days_ago=0)
        _spend(session, model="b", days_ago=10)
        today = timezone.localdate()

        params = {"spend_from": (today - timedelta(days=1)).isoformat(), "spend_to": today.isoformat()}

        assert _spend_pks({**params, "spend_model": "b"}) == set()
        assert _spend_pks({**params, "spend_model": "a"}) == {session.pk}

    @pytest.mark.parametrize(
        ("param", "value", "matching"),
        [
            ("spend_repo", "g/a", {"repo_id": "g/a"}),
            ("spend_trigger", SessionOrigin.SCHEDULE, {"trigger_type": SessionOrigin.SCHEDULE}),
            ("spend_task", SpendTask.TITLE, {"task": SpendTask.TITLE, "trigger_type": ""}),
            ("spend_model", "m", {"model": "m"}),
            ("spend_provider", "openrouter", {"provider_slug": "openrouter"}),
            ("spend_person", "xgitlab:7", {"external_provider": "gitlab", "external_uid": "7"}),
            ("spend_person", "daiv", {}),
        ],
    )
    def test_each_param_alone(self, user, param, value, matching):
        hit, miss = _create_session(), _create_session()
        _spend(hit, **matching)
        _spend(miss, repo_id="g/other", trigger_type=SessionOrigin.API_JOB, model="other", user=user)

        assert _spend_pks({param: value}) == {hit.pk}

    def test_the_person_of_a_user(self, user):
        hit, miss = _create_session(), _create_session()
        _spend(hit, user=user)
        _spend(miss)

        assert _spend_pks({"spend_person": f"u{user.pk}"}) == {hit.pk}

    @pytest.mark.parametrize(
        "params", [{"spend_trigger": "bogus"}, {"spend_task": "bogus"}, {"spend_from": "x"}, {"spend_person": "zz"}]
    )
    def test_an_invalid_value_is_dropped(self, user, params):
        spent, unspent = _create_session(), _create_session()
        _spend(spent)

        assert _spend_pks(params) == {spent.pk, unspent.pk}

    def test_an_invalid_person_still_keeps_the_other_spend_params(self, user):
        hit, miss = _create_session(), _create_session()
        _spend(hit, model="m")
        _spend(miss)

        assert _spend_pks({"spend_person": "zz", "spend_model": "m"}) == {hit.pk}

    @pytest.mark.parametrize("param", ["spend_model", "spend_from", "spend_person"])
    def test_a_spend_param_opens_on_everyone(self, param):
        assert resolve_scope({param: "x"}) == "all"
