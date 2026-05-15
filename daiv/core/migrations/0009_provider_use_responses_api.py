from django.db import migrations, models


def enable_for_openai_seed(apps, schema_editor):
    """Real OpenAI fully supports the Responses API, so the locked ``openai`` seed row
    keeps the pre-flag behavior. Other providers (Anthropic/Google/OpenRouter) ignore
    this flag, and custom rows default to ``False`` because most OpenAI-compatible
    servers only implement ``/v1/chat/completions``."""
    Provider = apps.get_model("core", "Provider")
    Provider.objects.filter(slug="openai", is_locked=True).update(use_responses_api=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0008_provider")]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="use_responses_api",
            field=models.BooleanField(default=False, verbose_name="use Responses API"),
        ),
        migrations.RunPython(enable_for_openai_seed, noop_reverse),
    ]
