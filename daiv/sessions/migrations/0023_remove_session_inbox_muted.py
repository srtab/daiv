from django.db import migrations, models

REASONS = ["not_daiv_commit", "already_examined", "budget_spent", "watch_off", "run_limit"]


def clear_muted_reasons(apps, schema_editor):
    """Events a muted session only remembered keep being remembered, with no reason the new constraint refuses."""
    InboxEvent = apps.get_model("agent_sessions", "InboxEvent")
    InboxEvent.objects.filter(reason="muted").update(reason="")


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0022_mergerequestownership_last_reconciled_at")]

    operations = [
        migrations.RemoveConstraint(model_name="inboxevent", name="inbox_event_reason_valid"),
        migrations.RemoveField(model_name="session", name="inbox_muted"),
        migrations.RunPython(clear_muted_reasons, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="inboxevent",
            name="reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("not_daiv_commit", "The commit is not one DAIV pushed"),
                    ("already_examined", "DAIV already examined this commit"),
                    ("budget_spent", "The CI attempts are spent"),
                    ("watch_off", "The CI watch is off"),
                    ("run_limit", "Too many runs in a row without a person"),
                ],
                default="",
                max_length=32,
                verbose_name="reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboxevent",
            constraint=models.CheckConstraint(
                condition=models.Q(reason="") | models.Q(reason__in=REASONS), name="inbox_event_reason_valid"
            ),
        ),
    ]
