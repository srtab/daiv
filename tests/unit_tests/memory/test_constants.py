from memory.constants import (
    CONSOLIDATION_MAX_PENDING_AGE_DAYS,
    CONSOLIDATION_MIN_PENDING,
    MEMORY_MAX_BYTES,
    MEMORY_MAX_LINES,
)
from memory.schemas import CONTENT_HARD_LIMIT


def test_task_default_constants_mirror_site_settings_defaults(monkeypatch):
    # The module constants are the documented defaults; they must equal the values the
    # site-settings layer serves so behavior is identical whether or not an admin overrode them.
    # Clear the env overrides too: site_settings checks them before the DB/default, so a stray
    # DAIV_MEMORY_* in the runner's environment would otherwise short-circuit this assertion.
    from unittest.mock import patch as _patch

    from core.models import SiteConfiguration
    from core.site_settings import site_settings

    for env_var in (
        "DAIV_MEMORY_MAX_LINES",
        "DAIV_MEMORY_MAX_BYTES",
        "DAIV_MEMORY_CONSOLIDATION_MIN_PENDING",
        "DAIV_MEMORY_CONSOLIDATION_MAX_PENDING_AGE_DAYS",
    ):
        monkeypatch.delenv(env_var, raising=False)

    with _patch.object(SiteConfiguration, "get_cached", return_value=None):
        assert site_settings.memory_max_lines == MEMORY_MAX_LINES
        assert site_settings.memory_max_bytes == MEMORY_MAX_BYTES
        assert site_settings.memory_consolidation_min_pending == CONSOLIDATION_MIN_PENDING
        assert site_settings.memory_consolidation_max_pending_age_days == CONSOLIDATION_MAX_PENDING_AGE_DAYS


def test_one_entry_at_the_hard_limit_fits_the_default_byte_budget():
    # Only true at the default: memory_max_bytes is operator-editable, and prune_to_budget never
    # evicts the last entry, so a single entry can still overshoot a budget an admin shrank.
    worst_case_utf8_bytes_per_char = 4
    assert CONTENT_HARD_LIMIT * worst_case_utf8_bytes_per_char <= MEMORY_MAX_BYTES
