from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory.models import RepositoryMemory

from automation.agent.middlewares.memory import MEMORY_SECTION_HEADER, RepositoryMemoryMiddleware
from codebase.repo_config import RepositoryConfig
from core.models import SiteConfiguration
from tests.unit_tests.conftest import agent_settings, site_snapshot


def _middleware(*, site_on: bool = True, repo_on: bool = True) -> RepositoryMemoryMiddleware:
    """The middleware as ``create_daiv_agent`` builds it for a run on a site and a repo with these memory switches."""
    settings = agent_settings(
        site=site_snapshot(memory_enabled=site_on), repo=RepositoryConfig(memory={"enabled": repo_on})
    )
    return RepositoryMemoryMiddleware(enabled=settings.features.memory)


def _request(*, system_prompt="BASE PROMPT", slug="group/project"):
    request = MagicMock()
    request.system_prompt = system_prompt
    request.runtime.context.repository.slug = slug
    overridden = MagicMock()
    request.override = MagicMock(return_value=overridden)
    return request, overridden


@pytest.mark.django_db(transaction=True)
async def test_injects_memory_section_into_system_prompt():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- never edit pyproject.toml")
    request, overridden = _request()
    handler = AsyncMock(return_value="response")

    result = await _middleware().awrap_model_call(request, handler)

    assert result == "response"
    injected = request.override.call_args.kwargs["system_prompt"]
    assert injected.startswith("BASE PROMPT")
    assert MEMORY_SECTION_HEADER in injected
    assert "never edit pyproject.toml" in injected
    handler.assert_awaited_once_with(overridden)


@pytest.mark.django_db(transaction=True)
async def test_noop_when_no_memory_exists():
    request, _ = _request()
    handler = AsyncMock(return_value="response")

    await _middleware().awrap_model_call(request, handler)

    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_noop_when_disabled_in_repo_config():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- something")
    request, _ = _request()
    handler = AsyncMock(return_value="response")

    await _middleware(repo_on=False).awrap_model_call(request, handler)

    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_noop_when_disabled_site_wide():
    # Repo flag is on and a memory document exists, but the instance-wide master switch
    # is off → the document must not be injected.
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- something")
    request, _ = _request()
    handler = AsyncMock(return_value="response")

    result = await _middleware(site_on=False).awrap_model_call(request, handler)

    assert result == "response"
    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_a_site_switch_turned_off_mid_run_leaves_that_runs_injection_on():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- something")
    middleware = _middleware()
    handler = AsyncMock(return_value="response")
    first, _ = _request()
    second, _ = _request()

    await middleware.awrap_model_call(first, handler)
    with patch.object(SiteConfiguration, "get_cached", return_value=SiteConfiguration(memory_enabled=False)):
        await middleware.awrap_model_call(second, handler)

    first.override.assert_called_once()
    second.override.assert_called_once()


@pytest.mark.django_db(transaction=True)
async def test_loads_memory_once_per_instance():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Workflow\n- use kebab-case branches")
    middleware = _middleware()
    handler = AsyncMock(return_value="response")

    request1, _ = _request()
    request2, _ = _request()
    await middleware.awrap_model_call(request1, handler)
    with patch("memory.models.RepositoryMemory.objects") as objects_mock:
        await middleware.awrap_model_call(request2, handler)
        objects_mock.filter.assert_not_called()
    request2.override.assert_called_once()


@pytest.mark.django_db(transaction=True)
async def test_never_raises_on_lookup_failure():
    request, _ = _request()
    handler = AsyncMock(return_value="response")
    middleware = _middleware()

    with patch("memory.models.RepositoryMemory.objects") as objects_mock:
        objects_mock.filter.side_effect = RuntimeError("db down")
        result = await middleware.awrap_model_call(request, handler)

    assert result == "response"
    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)
