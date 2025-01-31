# Generated by Django 5.1.4 on 2025-01-09 19:08

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import django_extensions.db.fields


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="APIKey",
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
                ("name", models.CharField(blank=True, max_length=128, verbose_name="name")),
                ("prefix", models.CharField(max_length=8, unique=True, verbose_name="prefix")),
                ("hashed_key", models.CharField(max_length=256, unique=True, verbose_name="API Key")),
                ("expires_at", models.DateTimeField(blank=True, null=True, verbose_name="expires at")),
                ("revoked", models.BooleanField(default=False, verbose_name="revoked")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="api_keys",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"verbose_name": "API Key", "verbose_name_plural": "API Keys"},
        )
    ]
