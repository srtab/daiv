from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_rocketchat_fields")]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="rocketchat_enabled",
            field=models.BooleanField(
                help_text="Offer Rocket Chat as a notification channel for users.",
                null=True,
                verbose_name="enable Rocket Chat",
            ),
        )
    ]
