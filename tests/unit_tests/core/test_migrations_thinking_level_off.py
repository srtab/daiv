import importlib

from django.apps import apps

import pytest

from core.models import SiteConfiguration

migration = importlib.import_module("core.migrations.0018_thinking_level_off")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "other"),
    [("agent_thinking_level", "agent_max_thinking_level"), ("agent_max_thinking_level", "agent_thinking_level")],
)
def test_a_stored_empty_thinking_level_keeps_meaning_the_default(field, other):
    SiteConfiguration.objects.filter(pk=SiteConfiguration.objects.get_instance().pk).update(**{
        field: "",
        other: "high",
    })

    migration.empty_thinking_levels_to_null(apps, None)

    config = SiteConfiguration.objects.get_instance()
    assert (getattr(config, field), getattr(config, other)) == (None, "high")
