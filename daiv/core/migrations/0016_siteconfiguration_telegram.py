from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0015_siteconfiguration_session_link_enabled")]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="telegram_enabled",
            field=models.BooleanField(
                help_text="Offer Telegram as a notification channel for users.",
                null=True,
                verbose_name="enable Telegram",
            ),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="telegram_bot_username",
            field=models.CharField(
                blank=True,
                help_text="Derived from the bot token via getMe; not editable here.",
                max_length=64,
                null=True,
                verbose_name="Telegram bot username",
            ),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="_telegram_bot_token_encrypted",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="_telegram_webhook_secret_encrypted",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
    ]
