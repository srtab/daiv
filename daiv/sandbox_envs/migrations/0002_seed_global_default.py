from django.db import migrations


def forwards(apps, schema_editor):
    from sandbox_envs.migrations._seed import seed_global_default

    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    seed_global_default(SandboxEnvironment)


def backwards(apps, schema_editor):
    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    SandboxEnvironment.objects.filter(scope="global", name="Default").delete()


class Migration(migrations.Migration):
    dependencies = [("sandbox_envs", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
