import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone

INBOX_TRIGGERS = ["chat", "mr_webhook", "api_job", "mcp_job", "delegation_resume", "pipeline_webhook"]
OPEN_WATCH_STATES = ("watching", "fixing")
SUPERSEDED_MESSAGE = (
    "Not started: the session inbox replaced the queue this run waited in, and another run was already queued on "
    "this branch. Submit it again."
)


def fail_extra_queued_runs(apps, schema_editor):
    """Fail every QUEUED run past the oldest per session, branch and user, which the old FIFO queue could stack and
    the new constraints forbid."""
    Run = apps.get_model("agent_sessions", "Run")
    queued = Run.objects.filter(status="QUEUED", trigger_type__in=INBOX_TRIGGERS)
    crowded = queued.values("session_id", "ref", "user_id").annotate(n=Count("id")).filter(n__gt=1)
    for group in crowded:
        extra = queued.filter(session_id=group["session_id"], ref=group["ref"], user_id=group["user_id"]).order_by(
            "created_at", "id"
        )[1:]
        Run.objects.filter(pk__in=[run.pk for run in extra]).update(
            status="FAILED", finished_at=timezone.now(), error_message=SUPERSEDED_MESSAGE
        )


def migrate_open_watches(apps, schema_editor):
    """Each open CI watch becomes an ownership owned by its merge request's own thread, so it finishes where it is.

    ``last_daiv_sha`` stays blank: a migration cannot ask the platform, and the wake rule counts a blank one as DAIV's.
    """
    Session = apps.get_model("agent_sessions", "Session")
    MergeRequestOwnership = apps.get_model("agent_sessions", "MergeRequestOwnership")
    watches = Session.objects.filter(watch_state__in=OPEN_WATCH_STATES, merge_request_iid__isnull=False)
    MergeRequestOwnership.objects.bulk_create(
        [
            MergeRequestOwnership(
                repo_id=session.repo_id,
                merge_request_iid=session.merge_request_iid,
                session_id=session.pk,
                owner_id=session.user_id,
                source_branch=session.ref,
                attempts=session.watch_attempts,
                last_pipeline_id=session.watch_pipeline_id,
            )
            for session in watches.iterator()
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("agent_sessions", "0020_run_publish_result"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="pushed_sha",
            field=models.CharField(blank=True, default="", max_length=64, verbose_name="pushed commit"),
        ),
        migrations.AddField(
            model_name="run",
            name="external_provider",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="external provider"),
        ),
        migrations.AddField(
            model_name="run",
            name="external_uid",
            field=models.CharField(blank=True, default="", max_length=191, verbose_name="external account ID"),
        ),
        migrations.AddField(
            model_name="session",
            name="inbox_muted",
            field=models.BooleanField(
                default=False,
                help_text="CI results and delegated batches are only remembered; people's requests still start runs.",
                verbose_name="inbox muted",
            ),
        ),
        migrations.AddField(
            model_name="session",
            name="external_provider",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="external provider"),
        ),
        migrations.AddField(
            model_name="session",
            name="external_uid",
            field=models.CharField(blank=True, default="", max_length=191, verbose_name="external account ID"),
        ),
        migrations.CreateModel(
            name="InboxEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
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
                        ],
                        max_length=32,
                        verbose_name="kind",
                    ),
                ),
                ("source_key", models.CharField(max_length=255, verbose_name="source key")),
                ("subject", models.CharField(blank=True, default="", max_length=255, verbose_name="subject")),
                ("ref", models.CharField(blank=True, default="", max_length=255, verbose_name="branch / ref")),
                (
                    "merge_request_iid",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="merge request IID"),
                ),
                ("wake", models.BooleanField(verbose_name="wakes the agent")),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("not_daiv_commit", "The commit is not one DAIV pushed"),
                            ("already_examined", "DAIV already examined this commit"),
                            ("budget_spent", "The CI attempts are spent"),
                            ("watch_off", "The CI watch is off"),
                            ("muted", "The session is muted"),
                            ("run_limit", "Too many runs in a row without a person"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="reason",
                    ),
                ),
                ("payload", models.JSONField(default=dict, verbose_name="payload")),
                (
                    "author_username",
                    models.CharField(blank=True, default="", max_length=255, verbose_name="author username"),
                ),
                (
                    "author_provider",
                    models.CharField(blank=True, default="", max_length=32, verbose_name="author's git platform"),
                ),
                (
                    "author_uid",
                    models.CharField(blank=True, default="", max_length=191, verbose_name="author's account ID"),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("taken", "Taken"),
                            ("handled", "Handled"),
                            ("dismissed", "Dismissed"),
                            ("superseded", "Superseded"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                        verbose_name="state",
                    ),
                ),
                ("claim_token", models.CharField(blank=True, default="", max_length=64, verbose_name="claim token")),
                ("attempts", models.PositiveSmallIntegerField(default=0, verbose_name="attempts")),
                (
                    "failure",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("access_denied", "The session owner can no longer run on this repository"),
                            ("retries_exhausted", "The runs that took it kept failing"),
                            ("session_gone", "The session no longer exists"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="failure",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False, verbose_name="created at"),
                ),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="author",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        blank=True,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="inbox_events",
                        to="agent_sessions.run",
                        verbose_name="run",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inbox_events",
                        to="agent_sessions.session",
                        verbose_name="session",
                    ),
                ),
            ],
            options={
                "verbose_name": "Inbox event",
                "verbose_name_plural": "Inbox events",
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(fields=["session", "state", "created_at"], name="inbox_session_state_idx"),
                    models.Index(
                        condition=models.Q(("state", "pending"), ("wake", True)),
                        fields=["session"],
                        name="inbox_pending_wake_idx",
                    ),
                    models.Index(condition=models.Q(("run__isnull", False)), fields=["run"], name="inbox_run_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("session", "source_key"), name="inbox_event_unique_source"),
                    models.CheckConstraint(
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
                            ],
                        )),
                        name="inbox_event_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q((
                            "state__in",
                            ["pending", "taken", "handled", "dismissed", "superseded", "failed"],
                        )),
                        name="inbox_event_state_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("reason", ""),
                            (
                                "reason__in",
                                [
                                    "not_daiv_commit",
                                    "already_examined",
                                    "budget_spent",
                                    "watch_off",
                                    "muted",
                                    "run_limit",
                                ],
                            ),
                            _connector="OR",
                        ),
                        name="inbox_event_reason_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("failure", ""),
                            ("failure__in", ["access_denied", "retries_exhausted", "session_gone"]),
                            _connector="OR",
                        ),
                        name="inbox_event_failure_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="MergeRequestOwnership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("repo_id", models.CharField(max_length=255, verbose_name="repository")),
                ("merge_request_iid", models.PositiveIntegerField(verbose_name="merge request IID")),
                ("source_branch", models.CharField(max_length=255, verbose_name="source branch")),
                ("watch_enabled", models.BooleanField(default=True, verbose_name="CI watch on")),
                ("attempts", models.PositiveSmallIntegerField(default=0, verbose_name="CI attempts")),
                (
                    "last_daiv_sha",
                    models.CharField(blank=True, default="", max_length=64, verbose_name="last commit DAIV pushed"),
                ),
                (
                    "last_examined_sha",
                    models.CharField(blank=True, default="", max_length=64, verbose_name="last commit examined"),
                ),
                (
                    "last_pipeline_id",
                    models.BigIntegerField(blank=True, null=True, verbose_name="last pipeline judged"),
                ),
                (
                    "last_pipeline_status",
                    models.CharField(blank=True, default="", max_length=32, verbose_name="last pipeline status judged"),
                ),
                ("closed_at", models.DateTimeField(blank=True, null=True, verbose_name="closed at")),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False, verbose_name="created at"),
                ),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="owner",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owned_merge_requests",
                        to="agent_sessions.session",
                        verbose_name="session",
                    ),
                ),
            ],
            options={
                "verbose_name": "Merge request ownership",
                "verbose_name_plural": "Merge request ownerships",
                "indexes": [
                    models.Index(
                        condition=models.Q(("closed_at__isnull", True)),
                        fields=["repo_id", "source_branch"],
                        name="mr_ownership_open_branch_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("repo_id", "merge_request_iid"), name="mr_ownership_unique_merge_request"
                    )
                ],
            },
        ),
        migrations.RunPython(fail_extra_queued_runs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "QUEUED"), ("trigger_type__in", INBOX_TRIGGERS), ("user__isnull", False)),
                fields=("session", "ref", "user"),
                name="run_one_next_run_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "QUEUED"), ("trigger_type__in", INBOX_TRIGGERS), ("user__isnull", True)),
                fields=("session", "ref", "external_provider", "external_uid"),
                name="run_one_next_run_per_account",
            ),
        ),
        # Last: its inserts queue deferred FK checks, and Postgres refuses DDL on the table after them.
        migrations.RunPython(migrate_open_watches, migrations.RunPython.noop),
    ]
