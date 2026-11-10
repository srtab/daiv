from django.db import migrations

GITLESS_IMAGE = "python:3.12-alpine"
IMAGE_WITH_GIT = "ghcr.io/astral-sh/uv:python3.14-bookworm"


def forwards(apps, schema_editor):
    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    SandboxEnvironment.objects.filter(scope="global", name="Default", base_image=GITLESS_IMAGE).update(
        base_image=IMAGE_WITH_GIT
    )


def backwards(apps, schema_editor):
    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    SandboxEnvironment.objects.filter(scope="global", name="Default", base_image=IMAGE_WITH_GIT).update(
        base_image=GITLESS_IMAGE
    )


class Migration(migrations.Migration):
    dependencies = [("sandbox_envs", "0006_drop_network_enabled")]
    operations = [migrations.RunPython(forwards, backwards)]
