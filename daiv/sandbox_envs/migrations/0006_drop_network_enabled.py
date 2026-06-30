from django.db import migrations


def null_egress_for_off_envs(apps, schema_editor):
    """Preserve 'off' intent before the column is dropped: an env that was network-off (or unset) but
    happens to carry a stored egress policy must not silently flip to networked. Null its policy.

    Runs BEFORE RemoveField, so the historical model still exposes ``network_enabled``."""
    SandboxEnvironment = apps.get_model("sandbox_envs", "SandboxEnvironment")
    SandboxEnvironment.objects.exclude(network_enabled=True).filter(egress_policy__isnull=False).update(
        egress_policy=None, _egress_secrets_encrypted=None
    )


def noop_reverse(apps, schema_editor):
    # No reverse: the network_enabled distinction is gone and cannot be reconstructed.
    pass


class Migration(migrations.Migration):
    dependencies = [("sandbox_envs", "0005_sandboxenvironment__egress_secrets_encrypted_and_more")]
    operations = [
        migrations.RunPython(null_egress_for_off_envs, noop_reverse),
        migrations.RemoveField(model_name="sandboxenvironment", name="network_enabled"),
    ]
