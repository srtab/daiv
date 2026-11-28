import django.db.models.deletion
from django.db import migrations, models

import django_extensions.db.fields


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0046_run_runs_alone_and_constraints")]

    operations = [
        migrations.CreateModel(
            name="SessionSubmodule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "created",
                    django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "modified",
                    django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name="modified"),
                ),
                (
                    "root_repo_id",
                    models.CharField(
                        help_text="The repository of the session whose checkout holds the submodule.",
                        max_length=255,
                        verbose_name="root repository",
                    ),
                ),
                (
                    "parent_repo_id",
                    models.CharField(
                        help_text="The repository whose commit records the submodule: the root, or an outer submodule's.",
                        max_length=255,
                        verbose_name="parent repository",
                    ),
                ),
                (
                    "path",
                    models.TextField(
                        help_text="The submodule's path from the top of the root's checkout.", verbose_name="path"
                    ),
                ),
                ("repo_id", models.CharField(max_length=255, verbose_name="repository")),
                ("ref", models.CharField(blank=True, default="", max_length=255, verbose_name="branch / ref")),
                (
                    "recorded_sha",
                    models.CharField(blank=True, default="", max_length=64, verbose_name="recorded commit"),
                ),
                (
                    "merge_pending",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Its merge request merged, and DAIV hasn't checked yet whether its parent needs the merged "
                            "commit."
                        ),
                        verbose_name="merge pending",
                    ),
                ),
                (
                    "merge_request_iid",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="merge request IID"),
                ),
                (
                    "merge_request_web_url",
                    models.URLField(blank=True, default="", max_length=500, verbose_name="merge request URL"),
                ),
                ("merge_request_title", models.TextField(blank=True, default="", verbose_name="merge request title")),
                ("merge_request_draft", models.BooleanField(default=False, verbose_name="merge request is a draft")),
                ("diff_stats", models.JSONField(blank=True, null=True, verbose_name="change size")),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="submodules",
                        to="agent_sessions.session",
                        verbose_name="session",
                    ),
                ),
            ],
            options={
                "verbose_name": "Session submodule",
                "verbose_name_plural": "Session submodules",
                "ordering": ["created", "pk"],
                "indexes": [models.Index(fields=["repo_id", "merge_request_iid"], name="session_submodule_mr_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("session", "repo_id"), name="session_submodule_unique")
                ],
            },
        )
    ]
