import pytest


@pytest.fixture(autouse=True)
def _clear_docker_secret_cache():
    from core.site_settings import _docker_secret_cache

    _docker_secret_cache.clear()
    yield
    _docker_secret_cache.clear()
