from typing import TYPE_CHECKING

import pytest

from core.models import WebFetchAuthHeader

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _clear_docker_secret_cache():
    from core.site_settings import _docker_secret_cache

    _docker_secret_cache.clear()
    yield
    _docker_secret_cache.clear()


@pytest.fixture
def make_auth_header() -> Callable[[str, str, str], WebFetchAuthHeader]:
    """Factory for creating ``WebFetchAuthHeader`` rows in tests.

    ``header_value`` is an :class:`EncryptedFieldDescriptor`, not a Django
    field, so it must be set after construction rather than passed to
    ``__init__``.
    """

    def _make(domain: str, header_name: str, header_value: str) -> WebFetchAuthHeader:
        row = WebFetchAuthHeader(domain=domain, header_name=header_name)
        row.header_value = header_value
        row.save()
        return row

    return _make
