import uuid
from decimal import Decimal
from importlib import import_module

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

muted_migration = import_module("sessions.migrations.0006_run_muted_classify_eligible")


def _run(status):
    """A run in its own session so the one-active-per-session constraint never fires."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="x/y")
    return Run.objects.create(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="x/y", status=status)


@pytest.mark.django_db
def test_backfill_ineligible_freezes_only_terminal_runs():
    """The pre-deploy freeze must catch terminal rows (SUCCESSFUL/FAILED) and leave a run that is still
    RUNNING/QUEUED across the deploy eligible, so it is still recoverable when it finishes."""
    from django.apps import apps as global_apps

    runs = {
        status: _run(status) for status in (RunStatus.SUCCESSFUL, RunStatus.FAILED, RunStatus.RUNNING, RunStatus.QUEUED)
    }
    assert all(r.classify_eligible for r in runs.values())  # AddField default is True

    muted_migration.backfill_ineligible(global_apps, None)

    for r in runs.values():
        r.refresh_from_db()
    assert runs[RunStatus.SUCCESSFUL].classify_eligible is False
    assert runs[RunStatus.FAILED].classify_eligible is False
    assert runs[RunStatus.RUNNING].classify_eligible is True
    assert runs[RunStatus.QUEUED].classify_eligible is True


open_watches_migration = import_module("sessions.migrations.0021_session_inbox")


@pytest.mark.django_db
def test_open_watches_become_ownerships_of_their_merge_request_thread(member_user):
    from django.apps import apps as global_apps

    from sessions.models import MergeRequestOwnership

    def _watch(state, iid):
        return Session.objects.create(
            thread_id=str(uuid.uuid4()),
            origin=SessionOrigin.PIPELINE_WEBHOOK,
            repo_id="group/repo",
            ref=f"daiv/branch-{iid}",
            merge_request_iid=iid,
            user=member_user,
            watch_state=state,
            watch_attempts=2,
            watch_pipeline_id=830724,
        )

    watching, fixing = _watch("watching", 1), _watch("fixing", 2)
    for state, iid in (("green", 3), ("exhausted", 4), ("off", 5)):
        _watch(state, iid)

    open_watches_migration.migrate_open_watches(global_apps, None)

    rows = {row.merge_request_iid: row for row in MergeRequestOwnership.objects.all()}
    assert set(rows) == {1, 2}
    for session in (watching, fixing):
        row = rows[session.merge_request_iid]
        assert (row.session_id, row.owner_id, row.source_branch) == (session.pk, member_user.pk, session.ref)
        assert (row.attempts, row.last_pipeline_id, row.last_daiv_sha) == (2, 830724, "")


@pytest.mark.django_db(transaction=True)
def test_a_session_its_owner_archived_stays_archived_and_other_archives_are_dropped(member_user, admin_user):
    from datetime import UTC, datetime

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = ("agent_sessions", "0033_archive_pending")
    executor = MigrationExecutor(connection)
    executor.migrate([before])
    try:
        old = executor.loader.project_state(before).apps
        sessions, hidden = old.get_model("agent_sessions", "Session"), old.get_model("agent_sessions", "HiddenSession")
        owners, merged = datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
        archived, kept, untouched = (
            sessions.objects.create(
                thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, user_id=member_user.pk, **fields
            )
            for fields in ({"archive_pending": True}, {"archived_at": merged}, {})
        )
        hidden.objects.create(session=archived, user_id=admin_user.pk, hidden_at=datetime(2026, 11, 1, tzinfo=UTC))
        hidden.objects.create(session=archived, user_id=member_user.pk, hidden_at=owners)
        hidden.objects.create(session=kept, user_id=member_user.pk, hidden_at=owners)
        hidden.objects.create(session=untouched, user_id=admin_user.pk, hidden_at=owners)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    rows = Session.objects.in_bulk([archived.pk, kept.pk, untouched.pk])
    assert (rows[archived.pk].archived_at, rows[archived.pk].archive_pending) == (owners, False)
    assert (rows[kept.pk].archived_at, rows[untouched.pk].archived_at) == (merged, None)


backfill_spend_migration = import_module("sessions.migrations.0039_backfill_spendrecord")


def _spent_run(member_user, **fields):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="g/r")
    return Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.CHAT,
        repo_id="g/r",
        status=RunStatus.SUCCESSFUL,
        user=member_user,
        agent_model="anthropic:claude-sonnet-4-6",
        **fields,
    )


@pytest.mark.django_db
class TestBackfillSpendRecords:
    def _backfill(self):
        from django.apps import apps as global_apps

        from sessions.models import SpendRecord

        backfill_spend_migration.backfill(global_apps, None)
        return {record.model: record for record in SpendRecord.objects.order_by("model")}

    def test_a_two_model_run_makes_two_rows_with_the_main_model_on_its_provider(self, member_user):
        run = _spent_run(
            member_user,
            input_tokens=1_500,
            usage_by_model={
                "claude-sonnet-4-6": {
                    "input_tokens": 1_000,
                    "output_tokens": 100,
                    "total_tokens": 1_100,
                    "input_token_details": {"cache_read": 400, "cache_creation": 10, "ephemeral_1h_input_tokens": 5},
                    "output_token_details": {"reasoning": 20},
                    "cost_usd": "0.012",
                },
                "claude-haiku-4-5": {
                    "input_tokens": 500,
                    "output_tokens": 50,
                    "total_tokens": 550,
                    "cost_usd": "0.001",
                },
            },
        )

        records = self._backfill()

        main, other = records["claude-sonnet-4-6"], records["claude-haiku-4-5"]
        assert (main.provider_slug, main.provider_name, other.provider_slug) == ("anthropic", "Anthropic", "")
        assert (main.cache_read_tokens, main.cache_write_tokens, main.reasoning_tokens) == (400, 15, 20)
        assert (main.cost_usd, main.reported_cost_usd, main.unpriced_tokens) == (Decimal("0.012"), None, 0)
        assert (main.cache_reported, main.cache_unpriced_tokens, main.cache_saving_usd) == (True, 400, 0)
        assert other.cache_reported is False
        assert (main.run_id, main.session_id, main.user_id, main.trigger_type) == (
            run.pk,
            run.session_id,
            member_user.pk,
            SessionOrigin.CHAT,
        )
        assert main.occurred_at == run.created_at

    def test_an_unpriced_model_counts_its_tokens_as_unpriced(self, member_user):
        _spent_run(member_user, input_tokens=10, usage_by_model={"acme/x": {"input_tokens": 10, "total_tokens": 12}})

        record = self._backfill()["acme/x"]

        assert (record.cost_usd, record.unpriced_tokens) == (0, 12)

    def test_a_doubled_model_name_is_repaired_and_merged(self, member_user):
        _spent_run(
            member_user,
            input_tokens=30,
            usage_by_model={
                "claude-sonnet-4-6claude-sonnet-4-6": {"input_tokens": 10, "total_tokens": 10, "cost_usd": "0.1"},
                "claude-sonnet-4-6": {"input_tokens": 20, "total_tokens": 20, "cost_usd": "0.2"},
            },
        )

        records = self._backfill()

        assert list(records) == ["claude-sonnet-4-6"]
        assert (records["claude-sonnet-4-6"].input_tokens, records["claude-sonnet-4-6"].cost_usd) == (
            30,
            Decimal("0.3"),
        )

    def test_a_run_with_totals_and_no_split_makes_one_unknown_row(self, member_user):
        _spent_run(member_user, input_tokens=7, output_tokens=3, total_tokens=10, cost_usd=None)

        record = self._backfill()["unknown"]

        assert (record.total_tokens, record.unpriced_tokens, record.provider_slug) == (10, 10, "")

    def test_a_run_without_usage_and_a_run_already_recorded_are_skipped(self, member_user):
        from sessions.models import SpendRecord

        _spent_run(member_user)
        recorded = _spent_run(
            member_user, input_tokens=1, usage_by_model={"claude-sonnet-4-6": {"input_tokens": 1, "total_tokens": 1}}
        )
        SpendRecord.objects.create(
            run=recorded,
            occurred_at=recorded.created_at,
            trigger_type=SessionOrigin.CHAT,
            model="claude-sonnet-4-6",
            provider_slug="anthropic",
        )

        self._backfill()

        assert SpendRecord.objects.count() == 1

    def test_the_reverse_deletes_only_backfilled_run_spend(self, member_user):
        from django.apps import apps as global_apps

        from sessions.models import SpendRecord, SpendTask

        run = _spent_run(member_user)
        shared = {"run": run, "occurred_at": run.created_at, "model": "m"}
        SpendRecord.objects.create(**shared, trigger_type=SessionOrigin.CHAT)
        live = SpendRecord.objects.create(
            **shared, trigger_type=SessionOrigin.CHAT, provider_slug="p", reported_cost_usd=0
        )
        background = SpendRecord.objects.create(**shared, task=SpendTask.TITLE)

        backfill_spend_migration.unbackfill(global_apps, None)

        assert set(SpendRecord.objects.values_list("pk", flat=True)) == {live.pk, background.pk}


counts_run_migration = import_module("sessions.migrations.0042_spendrecord_counts_run_deleted_user")


@pytest.mark.django_db
class TestCountsRunAndDeletedUsers:
    def test_one_run_spend_row_per_run_counts_it(self, member_user):
        from django.apps import apps as global_apps
        from django.utils import timezone

        from sessions.models import SpendRecord, SpendTask

        run = _run(RunStatus.SUCCESSFUL)
        spent = {"occurred_at": timezone.now(), "trigger_type": SessionOrigin.API_JOB}
        for model in ("m1", "m2"):
            SpendRecord.objects.create(run=run, model=model, **spent)
        SpendRecord.objects.create(run=run, model="m1", task=SpendTask.RUN_CLASSIFICATION, occurred_at=timezone.now())

        counts_run_migration.mark_counted_runs(global_apps, None)

        assert SpendRecord.objects.filter(counts_run=True).count() == 1
        assert SpendRecord.objects.get(counts_run=True).task == ""

    def test_rows_of_users_deleted_before_get_one_stand_in_id_per_name(self):
        from django.apps import apps as global_apps
        from django.utils import timezone

        from sessions.models import SpendRecord

        def _row(name, trigger=SessionOrigin.CHAT):
            return SpendRecord.objects.create(
                model="m", trigger_type=trigger, occurred_at=timezone.now(), person_name=name
            )

        ana, ana_again, bo = _row("Ana"), _row("Ana"), _row("Bo")
        nameless_chat, daiv = _row(""), _row("", SessionOrigin.PIPELINE_WEBHOOK)

        counts_run_migration.key_deleted_users(global_apps, None)

        rows = (ana, ana_again, bo, nameless_chat, daiv)
        ids = {row.pk: SpendRecord.objects.get(pk=row.pk).deleted_user_id for row in rows}
        assert ids[ana.pk] == ids[ana_again.pk] < 0
        assert ids[bo.pk] < 0 and ids[bo.pk] != ids[ana.pk]
        assert ids[nameless_chat.pk] == 0
        assert ids[daiv.pk] is None


merged_flag_migration = import_module("sessions.migrations.0043_backfill_mergerequestownership_merged")


@pytest.mark.django_db
def test_ownerships_closed_by_a_merge_before_the_merged_flag_are_marked_merged(member_user):
    from django.apps import apps as global_apps
    from django.utils import timezone

    from sessions.models import EventKind, InboxEvent, MergeRequestOwnership

    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", user=member_user
    )

    def _owned(repo_id, iid):
        return MergeRequestOwnership.objects.create(
            repo_id=repo_id,
            merge_request_iid=iid,
            session=session,
            source_branch=f"daiv/{iid}",
            closed_at=timezone.now(),
        )

    def _closed_by(kind, iid, repo_id=""):
        InboxEvent.objects.create(
            session=session,
            kind=kind,
            source_key=f"{kind}:{repo_id}:{iid}",
            merge_request_iid=iid,
            repo_id=repo_id,
            wake=False,
        )

    merged, closed, sibling_closed, sibling_merged = (
        _owned("group/repo", 1),
        _owned("group/repo", 2),
        _owned("group/repo", 3),
        _owned("group/other", 3),
    )
    _closed_by(EventKind.MR_MERGED, 1)
    _closed_by(EventKind.MR_CLOSED, 2)
    _closed_by(EventKind.MR_CLOSED, 3)
    _closed_by(EventKind.MR_MERGED, 3, repo_id="group/other")

    merged_flag_migration.backfill_merged(global_apps, None)

    states = {row.pk: row.merged for row in MergeRequestOwnership.objects.all()}
    assert states == {merged.pk: True, closed.pk: False, sibling_closed.pk: False, sibling_merged.pk: True}
