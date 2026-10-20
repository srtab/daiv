import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def move_content_type(apps, schema_editor, *, source="agent_sessions", target="notifications"):
    """Repoint the existing content type, so its permissions follow the model and none goes stale."""
    content_types = apps.get_model("contenttypes", "ContentType").objects.using(schema_editor.connection.alias)
    if not content_types.filter(app_label=target, model="runenvelope").exists():
        content_types.filter(app_label=source, model="runenvelope").update(app_label=target)


def restore_content_type(apps, schema_editor):
    move_content_type(apps, schema_editor, source="notifications", target="agent_sessions")


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0011_schedule_dispatch_failed_event"),
        ("agent_sessions", "0015_move_runenvelope_to_notifications"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="RunEnvelope",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("all-clear", "All clear"),
                                    ("found-issues", "Found issues"),
                                    ("needs-attention", "Needs attention"),
                                    ("needs-input", "Needs input"),
                                    ("failed", "Failed"),
                                ],
                                db_index=True,
                                max_length=16,
                                verbose_name="status",
                            ),
                        ),
                        ("count", models.PositiveIntegerField(default=0, verbose_name="count")),
                        ("summary", models.TextField(blank=True, default="", verbose_name="summary")),
                        ("actionable", models.JSONField(blank=True, default=list, verbose_name="actionable")),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now, editable=False, verbose_name="created at"
                            ),
                        ),
                        (
                            "run",
                            models.OneToOneField(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="envelope",
                                to="agent_sessions.run",
                                verbose_name="run",
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Run Envelope",
                        "verbose_name_plural": "Run Envelopes",
                        "db_table": "agent_sessions_runenvelope",
                        "ordering": ["-created_at", "-id"],
                        "constraints": [
                            models.CheckConstraint(
                                condition=models.Q((
                                    "status__in",
                                    ["all-clear", "found-issues", "needs-attention", "needs-input", "failed"],
                                )),
                                name="run_envelope_status_valid",
                            )
                        ],
                    },
                )
            ]
        ),
        migrations.RunPython(move_content_type, restore_content_type),
    ]
