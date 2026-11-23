from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0044_mergerequestownership_draft")]

    operations = [
        migrations.AddField(
            model_name="run",
            name="acts_as_user",
            field=models.BooleanField(
                default=True,
                help_text="Whether the run loads its user's personal tools and environments; false for CI identity "
                "runs.",
                verbose_name="acts as user",
            ),
        )
    ]
