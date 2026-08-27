import importlib

from django.db import migrations

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState


@pytest.mark.django_db
def test_a_new_session_is_not_watching():
    session = Session.objects.create(thread_id="t1", origin=SessionOrigin.CHAT, repo_id="group/repo")
    assert session.watch_state == WatchState.OFF
    assert session.watch_attempts == 0
    assert session.watch_pipeline_id is None
    assert session.watch_armed_at is None


def test_pipeline_webhook_is_not_in_the_issue_mr_webhook_set():
    # SessionOrigin.webhooks() is what existing callers branch on; a pipeline-triggered run
    # is not an issue/MR-comment webhook and must not silently join that set.
    assert SessionOrigin.PIPELINE_WEBHOOK not in SessionOrigin.webhooks()


def test_watch_states_are_exhaustive():
    assert set(WatchState.values) == {"off", "watching", "fixing", "green", "exhausted", "unclear"}


@pytest.mark.django_db
def test_a_pipeline_webhook_session_can_be_created():
    # Guards the session_origin_valid CheckConstraint against the new choice.
    session = Session.objects.create(thread_id="t2", origin=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo")
    assert session.origin == SessionOrigin.PIPELINE_WEBHOOK


@pytest.mark.django_db
def test_a_pipeline_webhook_run_can_be_created():
    # Guards the run_trigger_type_valid CheckConstraint against the new choice.
    session = Session.objects.create(thread_id="t3", origin=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo")
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo", status=RunStatus.READY
    )
    assert run.trigger_type == SessionOrigin.PIPELINE_WEBHOOK


def test_the_open_watch_index_literals_match_the_enum():
    """The model derives the condition from the enum but the migration froze it as literals, so a
    renamed state leaves the reconciler's partial index covering the old names — its sweeps degrade
    to a seq scan of every Session row rather than failing."""
    from sessions.migrations import __name__ as migrations_pkg

    module = importlib.import_module(f"{migrations_pkg}.0008_session_pipeline_watch")
    index = next(
        op.index
        for op in module.Migration.operations
        if isinstance(op, migrations.AddIndex) and op.index.name == "session_watch_sweep_idx"
    )
    (condition,) = index.condition.children

    assert set(condition[1]) == set(WatchState.open())
