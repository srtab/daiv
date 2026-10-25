from unittest.mock import patch

import pytest

from accounts.templatetags.consent_tags import social_consent_platform
from codebase.base import GitPlatform


class TestSocialConsentPlatform:
    @pytest.mark.parametrize(("capability_on", "expected"), [(True, "Gitlab"), (False, "")])
    def test_the_disclosure_follows_the_grant(self, capability_on, expected):
        with (
            patch("codebase.conf.settings") as codebase_mock,
            patch("core.site_settings.site_settings") as settings_mock,
        ):
            codebase_mock.CLIENT = GitPlatform.GITLAB
            settings_mock.cross_project_access_enabled = capability_on
            assert social_consent_platform() == expected

    def test_a_platform_without_oauth_says_nothing(self):
        with (
            patch("codebase.conf.settings") as codebase_mock,
            patch("core.site_settings.site_settings") as settings_mock,
        ):
            codebase_mock.CLIENT = GitPlatform.SWE
            settings_mock.cross_project_access_enabled = True
            assert social_consent_platform() == ""
