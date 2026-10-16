import importlib

from django.apps import apps
from django.db import migrations

import pytest

from core.models import SiteConfiguration

migration = importlib.import_module("core.migrations.0018_thinking_level_off")


@pytest.mark.django_db
def test_a_stored_empty_thinking_level_keeps_meaning_the_default():
    SiteConfiguration.objects.filter(pk=SiteConfiguration.objects.get_instance().pk).update(
        agent_thinking_level="", agent_max_thinking_level="high"
    )

    migration.empty_thinking_levels_to_null(apps, None)

    config = SiteConfiguration.objects.get_instance()
    assert (config.agent_thinking_level, config.agent_max_thinking_level) == (None, "high")


def test_the_data_migration_runs_before_the_fields_are_altered():
    operations = migration.Migration.operations
    data_migrations = [
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.RunPython) and operation.code is migration.empty_thinking_levels_to_null
    ]
    alterations = [index for index, operation in enumerate(operations) if isinstance(operation, migrations.AlterField)]

    assert len(data_migrations) == 1
    assert alterations
    assert data_migrations[0] < min(alterations)
