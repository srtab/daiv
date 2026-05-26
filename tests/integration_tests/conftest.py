import os

import pytest
import pytest_asyncio

from codebase.base import Scope
from codebase.context import set_runtime_ctx

_BUILT_IN_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# "google" is an alias parse_model_spec accepts; treat it as built-in so the
# discovery scan doesn't try to provision it as a custom provider.
_BUILT_IN_SLUGS = set(_BUILT_IN_PROVIDER_ENV) | {"google"}


def _discover_custom_slugs() -> set[str]:
    from .utils import CODING_MODEL_NAMES, FAST_MODEL_NAMES

    slugs: set[str] = set()
    for spec in (*CODING_MODEL_NAMES, *FAST_MODEL_NAMES):
        if ":" in spec:
            prefix = spec.split(":", 1)[0]
            if prefix not in _BUILT_IN_SLUGS:
                slugs.add(prefix)
    return slugs


@pytest.fixture(scope="session", autouse=True)
def _provision_providers(django_db_setup, django_db_blocker):
    """Wire real provider API keys from shell env into the test Provider table.

    The seed migration creates the four built-in rows with placeholder keys
    from pytest_env (e.g. OPENROUTER_API_KEY="test-key"). This fixture
    overwrites them with the real env value (or clears them if absent) and
    adds rows for custom providers discovered in the suite's model-name lists.
    Tests parametrized on a model whose env var isn't set will be skipped by
    ``require_provider_for_model`` in ``utils.py``.
    """
    from core.models import Provider

    with django_db_blocker.unblock():
        for slug, env_var in _BUILT_IN_PROVIDER_ENV.items():
            key = os.environ.get(env_var) or None
            row = Provider.objects.filter(slug=slug).first()
            if row is None:
                continue
            row.api_key = key
            row.is_enabled = bool(key)
            row.save()

        for slug in _discover_custom_slugs():
            prefix = f"DAIV_TEST_PROVIDER_{slug.upper()}"
            base_url = os.environ.get(f"{prefix}_BASE_URL")
            api_key = os.environ.get(f"{prefix}_API_KEY")
            if not base_url or not api_key:
                continue
            Provider.objects.update_or_create(
                slug=slug,
                defaults={
                    "display_name": os.environ.get(f"{prefix}_DISPLAY_NAME", slug.title()),
                    "provider_type": os.environ.get(f"{prefix}_TYPE", "openai"),
                    "base_url": base_url,
                    "api_key": api_key,
                    "is_enabled": True,
                },
            )

        Provider.invalidate_cache()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every integration test as needing DB access.

    Required so pytest-django's ``django_db_setup`` actually creates the test
    schema: by default it skips DB creation when no test asks for DB access
    via a marker or fixture, and our session-scoped ``_provision_providers``
    fixture's DB queries don't trigger the check.
    """
    for item in items:
        item.add_marker(pytest.mark.django_db)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def runtime_ctx():
    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        yield ctx
