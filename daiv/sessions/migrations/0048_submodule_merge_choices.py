from django.db import migrations, models

ORIGINS = [
    ("chat", "Chat"),
    ("api_job", "API Run"),
    ("mcp_job", "MCP Run"),
    ("schedule", "Scheduled Run"),
    ("ui_job", "Background Run"),
    ("issue_webhook", "Issue Webhook"),
    ("mr_webhook", "MR/PR Webhook"),
    ("pipeline_webhook", "Pipeline Webhook"),
    ("delegated_job", "Delegated Run"),
    ("delegation_resume", "Delegation Resume"),
    ("submodule_merge", "Submodule Merge"),
]
ORIGIN_VALUES = [value for value, _label in ORIGINS]
INBOX_TRIGGERS = [
    "chat",
    "mr_webhook",
    "api_job",
    "mcp_job",
    "delegation_resume",
    "pipeline_webhook",
    "submodule_merge",
]
EVENT_KINDS = [
    ("chat_message", "Chat message"),
    ("mention", "Mention"),
    ("follow_up_job", "Follow-up job"),
    ("delegation_finished", "Delegated jobs finished"),
    ("ci_failed", "CI failed"),
    ("ci_passed", "CI passed"),
    ("ci_stopped", "CI stopped"),
    ("branch_pushed", "Branch pushed"),
    ("foreign_publish", "Published by another session"),
    ("mr_merged", "Merge request merged"),
    ("mr_closed", "Merge request closed"),
    ("comment", "Comment"),
    ("delegated_ci_outcome", "Delegated session CI"),
    ("notice", "Notice"),
    ("leg_follow_up", "Leg follow-up"),
    ("merge_request_job", "Merge request job"),
    ("submodule_merged", "Submodule merged"),
]


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0047_sessionsubmodule")]

    operations = [
        migrations.RemoveConstraint(model_name="inboxevent", name="inbox_event_kind_valid"),
        migrations.RemoveConstraint(model_name="run", name="run_trigger_type_valid"),
        migrations.RemoveConstraint(model_name="run", name="run_one_next_run_per_user"),
        migrations.RemoveConstraint(model_name="run", name="run_one_next_run_per_account"),
        migrations.RemoveConstraint(model_name="session", name="session_origin_valid"),
        migrations.AlterField(
            model_name="inboxevent",
            name="kind",
            field=models.CharField(choices=EVENT_KINDS, max_length=32, verbose_name="kind"),
        ),
        migrations.AlterField(
            model_name="run",
            name="trigger_type",
            field=models.CharField(choices=ORIGINS, max_length=20, verbose_name="trigger type"),
        ),
        migrations.AlterField(
            model_name="session",
            name="origin",
            field=models.CharField(choices=ORIGINS, max_length=20, verbose_name="origin"),
        ),
        migrations.AlterField(
            model_name="spendrecord",
            name="trigger_type",
            field=models.CharField(blank=True, choices=ORIGINS, default="", max_length=20, verbose_name="trigger type"),
        ),
        migrations.AddConstraint(
            model_name="inboxevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", [value for value, _label in EVENT_KINDS])),
                name="inbox_event_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("runs_alone", False),
                    ("status", "QUEUED"),
                    ("trigger_type__in", INBOX_TRIGGERS),
                    ("user__isnull", False),
                ),
                fields=("session", "ref", "user"),
                name="run_one_next_run_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("runs_alone", False),
                    ("status", "QUEUED"),
                    ("trigger_type__in", INBOX_TRIGGERS),
                    ("user__isnull", True),
                ),
                fields=("session", "ref", "external_provider", "external_uid"),
                name="run_one_next_run_per_account",
            ),
        ),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.CheckConstraint(
                condition=models.Q(("trigger_type__in", ORIGIN_VALUES)), name="run_trigger_type_valid"
            ),
        ),
        migrations.AddConstraint(
            model_name="session",
            constraint=models.CheckConstraint(
                condition=models.Q(("origin__in", ORIGIN_VALUES)), name="session_origin_valid"
            ),
        ),
    ]
