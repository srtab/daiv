from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0009_provider_use_responses_api")]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="verify_ssl",
            field=models.BooleanField(default=True, verbose_name="verify TLS certificates"),
        )
    ]
