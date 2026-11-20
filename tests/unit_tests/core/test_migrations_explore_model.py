import importlib
import logging
from types import SimpleNamespace
from unittest.mock import Mock

migration = importlib.import_module("core.migrations.0024_explore_model_to_subagent_models")


def _site(**fields) -> SimpleNamespace:
    """A ``SiteConfiguration`` row as the historical model at 0020 holds it."""
    fields = {
        "agent_subagent_models": {},
        "agent_explore_model_name": None,
        "agent_explore_fallback_model_name": None,
        **fields,
    }
    return SimpleNamespace(**fields, save=Mock())


def _apps(site: SimpleNamespace) -> Mock:
    model = Mock()
    model.objects.all.return_value = [site]
    return Mock(get_model=Mock(return_value=model))


def test_a_saved_explore_model_becomes_the_explore_entry():
    site = _site(agent_explore_model_name="openrouter:x/explore", agent_subagent_models={"cr-tests": "openrouter:x/t"})

    migration.explore_model_to_subagent_models(_apps(site), None)

    assert site.agent_subagent_models == {"cr-tests": "openrouter:x/t", "explore": "openrouter:x/explore"}
    site.save.assert_called_once_with(update_fields=["agent_subagent_models"])


def test_an_existing_explore_entry_is_kept():
    site = _site(agent_explore_model_name="openrouter:x/old", agent_subagent_models={"explore": "openrouter:x/new"})

    migration.explore_model_to_subagent_models(_apps(site), None)

    assert site.agent_subagent_models == {"explore": "openrouter:x/new"}
    site.save.assert_not_called()


def test_no_saved_explore_model_leaves_the_map_alone():
    site = _site()

    migration.explore_model_to_subagent_models(_apps(site), None)

    assert site.agent_subagent_models == {}
    site.save.assert_not_called()


def test_a_dropped_explore_fallback_model_is_logged(caplog):
    site = _site(agent_explore_fallback_model_name="openrouter:x/fallback")

    with caplog.at_level(logging.WARNING, logger="daiv.core"):
        migration.explore_model_to_subagent_models(_apps(site), None)

    assert "openrouter:x/fallback" in caplog.text
