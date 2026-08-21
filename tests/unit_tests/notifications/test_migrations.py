from datetime import timedelta
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

import pytest

from accounts.models import User

dedup_migration = import_module("notifications.migrations.0007_per_run_notification_dedup")

# State just before 0007 — the widened unique constraint does not exist yet, so duplicate per-run
# rows (which the backfill must delete) can be created.
PRE_DEDUP = "0006_alter_notification_context"


def _migrate_to_head():
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes("notifications"))


@pytest.fixture
def at_pre_dedup():
    """Roll notifications back to ``PRE_DEDUP``, yield its historical apps, and restore head after —
    in a fixture so a failing assertion can't strand the suite at an old schema."""
    MigrationExecutor(connection).migrate([("notifications", PRE_DEDUP)])
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    try:
        yield executor.loader.project_state((("notifications", PRE_DEDUP),)).apps
    finally:
        _migrate_to_head()


@pytest.mark.django_db(transaction=True)
def test_dedup_keeps_earliest_per_source_recipient_and_spares_distinct_keys(at_pre_dedup):
    Notification = at_pre_dedup.get_model("notifications", "Notification")
    user = User.objects.create_user(username="dedup", email="dedup@test.com", password="x")  # noqa: S106

    dup = {"recipient_id": user.pk, "source_type": "sessions.Run", "source_id": "1", "event_type": "job.finished"}
    older = Notification.objects.create(subject="older", body="b", **dup)
    Notification.objects.create(subject="newer", body="b", **dup)
    Notification.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(hours=1))
    # Shares everything but source_id — dedup keys on the full tuple, so it must survive.
    distinct = Notification.objects.create(
        recipient_id=user.pk,
        source_type="sessions.Run",
        source_id="2",
        event_type="job.finished",
        subject="distinct",
        body="b",
    )

    dedup_migration.dedup_per_run_notifications(at_pre_dedup, None)

    assert set(Notification.objects.values_list("pk", flat=True)) == {older.pk, distinct.pk}


@pytest.mark.django_db(transaction=True)
def test_dedup_covers_schedule_finished_too(at_pre_dedup):
    Notification = at_pre_dedup.get_model("notifications", "Notification")
    user = User.objects.create_user(username="dedup2", email="dedup2@test.com", password="x")  # noqa: S106

    dup = {"recipient_id": user.pk, "source_type": "sessions.Run", "source_id": "7", "event_type": "schedule.finished"}
    keep = Notification.objects.create(subject="keep", body="b", **dup)
    Notification.objects.create(subject="drop", body="b", **dup)
    Notification.objects.filter(pk=keep.pk).update(created=timezone.now() - timedelta(hours=1))

    dedup_migration.dedup_per_run_notifications(at_pre_dedup, None)

    assert list(Notification.objects.values_list("pk", flat=True)) == [keep.pk]
