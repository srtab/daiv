import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Min


def mark_counted_runs(apps, schema_editor):
    SpendRecord = apps.get_model("agent_sessions", "SpendRecord")
    first = SpendRecord.objects.filter(task="", run__isnull=False).values("run").annotate(first=Min("pk"))
    SpendRecord.objects.filter(pk__in=first.values("first")).update(counts_run=True)


PERSON_TRIGGERS = ("chat", "api_job", "mcp_job", "ui_job", "schedule")


def key_deleted_users(apps, schema_editor):
    """Rows whose user was deleted before ids were stamped: one stand-in id per recorded name, below any real id, and
    id 0 for runs only a person starts whose user was gone before spend was recorded."""
    SpendRecord = apps.get_model("agent_sessions", "SpendRecord")
    orphans = SpendRecord.objects.filter(user__isnull=True, deleted_user_id__isnull=True, external_uid="")
    named = orphans.exclude(person_name="")
    names = named.order_by("person_name").values_list("person_name", flat=True).distinct()
    for index, name in enumerate(list(names), start=1):
        named.filter(person_name=name).update(deleted_user_id=-index)
    orphans.filter(person_name="", task="", trigger_type__in=PERSON_TRIGGERS).update(deleted_user_id=0)


class Migration(migrations.Migration):
    dependencies = [
        ("agent_sessions", "0041_session_origin_background_run"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="spendrecord",
            name="counts_run",
            field=models.BooleanField(default=False, verbose_name="counts its run"),
        ),
        migrations.AddField(
            model_name="spendrecord",
            name="deleted_user_id",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="deleted user ID"),
        ),
        migrations.AlterField(
            model_name="spendrecord",
            name="session",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="spend_records",
                to="agent_sessions.session",
                verbose_name="session",
            ),
        ),
        migrations.AlterField(
            model_name="spendrecord",
            name="user",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="spend_records",
                to=settings.AUTH_USER_MODEL,
                verbose_name="user",
            ),
        ),
        migrations.RunPython(mark_counted_runs, migrations.RunPython.noop),
        migrations.RunPython(key_deleted_users, migrations.RunPython.noop),
    ]
