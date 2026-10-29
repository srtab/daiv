import logging

from django.db import migrations

logger = logging.getLogger("daiv.core")


def explore_model_to_subagent_models(apps, schema_editor):
    SiteConfiguration = apps.get_model("core", "SiteConfiguration")
    for site in SiteConfiguration.objects.all():
        if site.agent_explore_model_name and "explore" not in site.agent_subagent_models:
            site.agent_subagent_models = {**site.agent_subagent_models, "explore": site.agent_explore_model_name}
            site.save(update_fields=["agent_subagent_models"])
        if site.agent_explore_fallback_model_name:
            logger.warning(
                "Dropped the explore fallback model '%s': the explore subagent now falls back to the agent's "
                "fallback models.",
                site.agent_explore_fallback_model_name,
            )


class Migration(migrations.Migration):
    dependencies = [("core", "0020_agent_skill_subagent_models")]

    operations = [
        migrations.RunPython(explore_model_to_subagent_models, migrations.RunPython.noop),
        migrations.RemoveField(model_name="siteconfiguration", name="agent_explore_model_name"),
        migrations.RemoveField(model_name="siteconfiguration", name="agent_explore_fallback_model_name"),
    ]
