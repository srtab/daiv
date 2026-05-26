"""Tests for the adapter registry."""

import pytest

from automation.agent.model_catalog.exceptions import UnsupportedProviderTypeError
from automation.agent.model_catalog.registry import get_adapter
from core.models import ProviderType


def test_registry_covers_all_provider_types():
    """Every ProviderType must have an adapter; missing entries surface as runtime errors."""
    for provider_type in ProviderType:
        adapter = get_adapter(provider_type)
        assert adapter is not None


def test_get_adapter_raises_on_unknown_type():
    with pytest.raises(UnsupportedProviderTypeError):
        get_adapter("totally-bogus-provider-type")  # type: ignore[arg-type]
