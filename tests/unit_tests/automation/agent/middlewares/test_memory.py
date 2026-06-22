from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory.models import RepositoryMemory

from automation.agent.middlewares.memory import MEMORY_SECTION_HEADER, RepositoryMemoryMiddleware


def _request(*, enabled=True, system_prompt="BASE PROMPT", slug="group/project"):
    request = MagicMock()
    request.system_prompt = system_prompt
    request.runtime.context.config.memory.enabled = enabled
    request.runtime.context.repository.slug = slug
    overridden = MagicMock()
    request.override = MagicMock(return_value=overridden)
    return request, overridden


@pytest.mark.django_db(transaction=True)
async def test_injects_memory_section_into_system_prompt():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- never edit pyproject.toml")
    request, overridden = _request()
    handler = AsyncMock(return_value="response")

    result = await RepositoryMemoryMiddleware().awrap_model_call(request, handler)

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

    await RepositoryMemoryMiddleware().awrap_model_call(request, handler)

    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_noop_when_disabled_in_repo_config():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- something")
    request, _ = _request(enabled=False)
    handler = AsyncMock(return_value="response")

    await RepositoryMemoryMiddleware().awrap_model_call(request, handler)

    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_noop_when_disabled_site_wide():
    # Repo flag is on and a memory document exists, but the instance-wide master switch
    # is off → the document must not be injected.
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- something")
    request, _ = _request(enabled=True)
    handler = AsyncMock(return_value="response")

    with patch("automation.agent.middlewares.memory.site_settings") as ss:
        ss.memory_enabled = False
        result = await RepositoryMemoryMiddleware().awrap_model_call(request, handler)

    assert result == "response"
    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)


@pytest.mark.django_db(transaction=True)
async def test_loads_memory_once_per_instance():
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Workflow\n- use kebab-case branches")
    middleware = RepositoryMemoryMiddleware()
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
    middleware = RepositoryMemoryMiddleware()

    with patch("memory.models.RepositoryMemory.objects") as objects_mock:
        objects_mock.filter.side_effect = RuntimeError("db down")
        result = await middleware.awrap_model_call(request, handler)

    assert result == "response"
    request.override.assert_not_called()
    handler.assert_awaited_once_with(request)
