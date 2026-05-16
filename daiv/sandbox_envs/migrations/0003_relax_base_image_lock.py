from django.db import migrations


def forwards(apps, schema_editor):
    from sandbox_envs.migrations._relax_base_image_lock import relax_base_image_lock

    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    relax_base_image_lock(SandboxEnvironment)


class Migration(migrations.Migration):
    dependencies = [("sandbox_envs", "0002_seed_global_default")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
