from django.conf import settings
from django.db import migrations

from langgraph.checkpoint.postgres import PostgresSaver


def initialize_postgres_saver(apps, schema_editor):
    with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
        checkpointer.setup()


class Migration(migrations.Migration):
    dependencies = []

    operations = [migrations.RunPython(initialize_postgres_saver, reverse_code=migrations.RunPython.noop)]
