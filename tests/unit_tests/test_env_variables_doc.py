"""The environment variables page claims to list every supported variable.

Every `SiteConfiguration` field can be set from the environment as
`DAIV_<FIELD_NAME>` (see `site_settings.get_env_var_name`), so this test holds
the page to that claim: each field's variable name must appear in
`docs/reference/env-variables.md`. It lives at the top of tests/unit_tests/
rather than mirroring an app package, like test_docs_links.py, because it
covers a docs page against the codebase, not a daiv module.
"""

from __future__ import annotations

from pathlib import Path

from core.models import SiteConfiguration
from core.site_settings import site_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VARIABLES_PAGE = REPO_ROOT / "docs" / "reference" / "env-variables.md"

# Fields deliberately left off the page, each with its reason: only when documenting
# one would be wrong, not merely unwritten. Anything else fails below until it has a row.
UNDOCUMENTED_FIELDS: dict[str, str] = {}


def test_every_site_setting_env_var_is_documented():
    page = ENV_VARIABLES_PAGE.read_text(encoding="utf-8")
    field_names = {*site_settings.FIELD_DEFAULTS, *SiteConfiguration.ENCRYPTED_FIELDS}
    missing = sorted(
        env_name
        for name in field_names
        if name not in UNDOCUMENTED_FIELDS
        if (env_name := site_settings.get_env_var_name(name)) not in page
    )
    assert not missing, (
        "These site settings can be set by environment variable but have no row in "
        "docs/reference/env-variables.md (add a row, or record the field in "
        "UNDOCUMENTED_FIELDS with a reason):\n" + "\n".join(missing)
    )
