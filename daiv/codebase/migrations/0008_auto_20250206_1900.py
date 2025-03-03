# Generated by Django 5.1.5 on 2025-02-06 19:00

from django.conf import settings
from django.db import migrations

from langgraph.checkpoint.postgres import PostgresSaver


def initialize_postgres_saver(apps, schema_editor):
    with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
        checkpointer.setup()


class Migration(migrations.Migration):
    dependencies = [("codebase", "0007_remove_codebasedocument_source_hnsw_index_and_more")]

    operations = [migrations.RunPython(initialize_postgres_saver, reverse_code=migrations.RunPython.noop)]
