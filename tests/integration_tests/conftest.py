import os
from pathlib import Path

import pytest
import pytest_asyncio

from codebase.base import Scope
from codebase.context import set_runtime_ctx

_HERE = Path(__file__).parent

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
            # api_key is an EncryptedFieldDescriptor, not a real field, so it
            # can't go in update_or_create's defaults — set it on the instance.
            row, _ = Provider.objects.get_or_create(slug=slug, defaults={"provider_type": "openai"})
            row.display_name = os.environ.get(f"{prefix}_DISPLAY_NAME", slug.title())
            row.provider_type = os.environ.get(f"{prefix}_TYPE", "openai")
            row.base_url = base_url
            row.api_key = api_key
            row.verify_ssl = os.environ.get(f"{prefix}_VERIFY_SSL", "true").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            row.is_enabled = True
            row.save()

        Provider.invalidate_cache()


@pytest.fixture(scope="session")
def _provider_snapshot(_provision_providers, django_db_blocker) -> list[dict]:
    """Snapshot every ``Provider`` row (with its real keys) once ``_provision_providers`` has run.

    ``django_db(transaction=True)`` teardown runs Django's ``flush``, which truncates
    ``core_provider`` — the four built-in rows come from a data migration that ``flush`` does not
    re-run, and this fixture (session-scoped) never runs a second time to reseed them. Captured as
    plain dicts (via ``.values()``, including ``_api_key_encrypted`` ciphertext and ``id``) so
    ``_restore_providers`` can recreate exactly what was there without re-deriving keys.
    """
    from core.models import Provider

    with django_db_blocker.unblock():
        rows = list(Provider.objects.values())

    # _restore_providers keys off "which snapshot rows are missing" -- an empty snapshot would
    # make that check permanently vacuous and every test pass on a silently empty provider table.
    assert rows, (
        "Provider table is empty at session start; the seed migration "
        "(daiv/core/migrations/0008_provider.py) did not run."
    )
    return rows


@pytest.fixture(autouse=True)
def _restore_providers(_provider_snapshot, django_db_blocker) -> None:
    """Reseed any ``Provider`` row a prior ``transaction=True`` test's flush removed.

    Runs before every test in this directory (every item here already carries a ``django_db``
    marker via ``pytest_collection_modifyitems`` below), so a missing row is recreated before the
    test body can observe an empty table.

    The read-and-write both happen on a *separate connection*, in a worker thread, not on the
    caller's own connection: a plain (non-``transaction=True``) test wraps its whole body — and
    every fixture that runs inside it — in one open transaction on the main connection, so an
    INSERT there would never commit. It is not enough to move only the write: a bare SELECT on
    the main connection already takes a lock inside that open transaction, and a second
    connection's INSERT then hits ``sqlite3.OperationalError: database table is locked`` anyway —
    so the existence check has to happen on the same worker-thread connection too. A brand new
    thread gets Django's default per-thread connection, which is a fresh, unwrapped, autocommit
    connection, so whatever it writes commits immediately regardless of the caller's own
    transaction state — exactly the same "separate connection from a worker thread" shape
    ``Provider._executor`` already uses for the identical reason (``daiv/core/models.py``).
    ``bulk_create`` bypasses ``Provider.save()``'s ``on_commit`` cache invalidation, so the cache
    is invalidated explicitly, but only when a row was actually recreated — an unconditional
    invalidate would otherwise evict a perfectly good warm cache on every single test.
    """
    import concurrent.futures

    from core.models import Provider

    def _restore() -> bool:
        from django.db import close_old_connections

        close_old_connections()
        try:
            existing = set(Provider.objects.values_list("slug", flat=True))
            missing = [row for row in _provider_snapshot if row["slug"] not in existing]
            if missing:
                Provider.objects.bulk_create(Provider(**row) for row in missing)
            return bool(missing)
        finally:
            close_old_connections()

    with django_db_blocker.unblock(), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        created = executor.submit(_restore).result()

    if created:
        Provider.invalidate_cache()


_MISSING_KEY_REASON = (
    "OPENROUTER_API_KEY is not set. Export it, or add it to docker/local/app/config.secrets.env "
    "(loaded by the --envfile flag in `make integration-tests`)."
)

_EMPTY_SELECTION_REASON = (
    "A -m expression deselected every integration test. pytest does not validate -m names against "
    "registered markers, so a typo deselects everything and exits 5 (NO_TESTS_COLLECTED) with no "
    "indication the marker name was wrong — this suite names the cause instead. "
    "Valid markers for this suite: diff_to_metadata, memory, sandbox, skills, deferred_frozen."
)


def _collected_integration_paths(config: pytest.Config) -> bool:
    """Whether this invocation was pointed at the integration suite at all.

    ``items`` at ``trylast`` has already lost the deselected items, so it cannot distinguish a run
    that asked for no integration tests from one whose ``-m`` deselected them all.
    """
    return any(
        Path(arg.split("::")[0]).resolve() == _HERE or _HERE in Path(arg.split("::")[0]).resolve().parents
        for arg in config.args
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every integration test as needing DB access, and refuse to run this suite blind.

    Required so pytest-django's ``django_db_setup`` actually creates the test
    schema: by default it skips DB creation when no test asks for DB access
    via a marker or fixture, and our session-scoped ``_provision_providers``
    fixture's DB queries don't trigger the check.

    Every model routes through OpenRouter, so without that key
    ``require_provider_for_model`` skips every test and pytest exits 0 — which is how this suite
    once sat unrunnable and green. A run that is *only* this suite therefore fails outright, while
    a wider run (bare ``pytest``, which ``testpaths`` points at all of ``tests/``) keeps its unit
    tests and skips these with the reason attached. ``trylast`` so marker deselection has already
    happened and ``-m`` narrowing is visible here.
    """
    ours = [item for item in items if _HERE in item.path.parents]
    for item in ours:
        item.add_marker(pytest.mark.django_db)

    # A -m that deselected everything: pytest exits 5 (NO_TESTS_COLLECTED) with no explanation, so
    # a typo in the Makefile's marker expression would fail opaquely instead of naming the cause.
    if not ours and config.option.markexpr and _collected_integration_paths(config):
        raise pytest.UsageError(_EMPTY_SELECTION_REASON)

    if ours and not os.environ.get("OPENROUTER_API_KEY"):
        if len(ours) == len(items):
            raise pytest.UsageError(_MISSING_KEY_REASON)
        for item in ours:
            item.add_marker(pytest.mark.skip(reason=_MISSING_KEY_REASON))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def runtime_ctx():
    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        yield ctx


def pytest_terminal_summary(terminalreporter) -> None:
    """Print the per-case-per-model vote split the memory suites recorded.

    The whole design rests on a later "it moved" claim being checkable, which needs the raw split
    and not just pass/fail.
    """
    from .memory_grading import votes_report

    for line in votes_report():
        terminalreporter.write_line(line)
