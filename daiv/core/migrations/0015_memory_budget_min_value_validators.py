import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0014_siteconfiguration_memory_consolidation_max_pending_age_days")]

    operations = [
        migrations.AlterField(
            model_name="siteconfiguration",
            name="memory_max_bytes",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Cap on the memory document size in bytes; entries beyond it are evicted.",
                null=True,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="memory max bytes",
            ),
        ),
        migrations.AlterField(
            model_name="siteconfiguration",
            name="memory_max_lines",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Cap on the memory document length in lines; entries beyond it are evicted.",
                null=True,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="memory max lines",
            ),
        ),
    ]
