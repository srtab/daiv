from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("notifications", "0007_per_run_notification_dedup")]

    operations = [
        migrations.AlterField(
            model_name="notificationdelivery",
            name="channel_type",
            field=models.CharField(
                choices=[("email", "Email"), ("rocketchat", "Rocket Chat"), ("telegram", "Telegram")],
                max_length=32,
                verbose_name="channel type",
            ),
        ),
        migrations.AlterField(
            model_name="userchannelbinding",
            name="channel_type",
            field=models.CharField(
                choices=[("email", "Email"), ("rocketchat", "Rocket Chat"), ("telegram", "Telegram")],
                max_length=32,
                verbose_name="channel type",
            ),
        ),
    ]
