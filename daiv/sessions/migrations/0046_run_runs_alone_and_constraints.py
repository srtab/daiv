from django.db import migrations, models

INBOX_TRIGGERS = ["chat", "mr_webhook", "api_job", "mcp_job", "delegation_resume", "pipeline_webhook"]


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0045_run_acts_as_user")]

    operations = [
        migrations.RemoveConstraint(model_name="run", name="run_one_active_per_session"),
        migrations.RemoveConstraint(model_name="run", name="run_one_next_run_per_user"),
        migrations.RemoveConstraint(model_name="run", name="run_one_next_run_per_account"),
        migrations.RemoveConstraint(model_name="inboxevent", name="inbox_event_kind_valid"),
        migrations.AlterField(
            model_name="inboxevent",
            name="kind",
            field=models.CharField(
                choices=[
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
                ],
                max_length=32,
                verbose_name="kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboxevent",
            constraint=models.CheckConstraint(
                condition=models.Q((
                    "kind__in",
                    [
                        "chat_message",
                        "mention",
                        "follow_up_job",
                        "delegation_finished",
                        "ci_failed",
                        "ci_passed",
                        "ci_stopped",
                        "branch_pushed",
                        "foreign_publish",
                        "mr_merged",
                        "mr_closed",
                        "comment",
                        "delegated_ci_outcome",
                        "notice",
                        "leg_follow_up",
                        "merge_request_job",
                    ],
                )),
                name="inbox_event_kind_valid",
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="runs_alone",
            field=models.BooleanField(
                default=False,
                help_text="Created with the one inbox event it runs, so it is never a next run other events join.",
                verbose_name="runs alone",
            ),
        ),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("merge_request_iid__isnull", True),
                    ("status__in", ["READY", "RUNNING"]),
                    ("trigger_type__in", ["api_job", "mcp_job", "delegated_job", "delegation_resume"]),
                ),
                fields=("session",),
                name="run_one_active_per_session",
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
    ]
