from django.db import migrations


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("sandbox_envs", "0002_seed_global_default")]
    operations = [migrations.RunPython(noop, reverse_code=migrations.RunPython.noop)]
