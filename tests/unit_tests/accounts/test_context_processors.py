from unittest.mock import patch

import pytest

from codebase.base import GitPlatform


class TestSocialConsent:
    @pytest.mark.parametrize(("capability_on", "expected"), [(True, {"socialaccount_platform": "Gitlab"}), (False, {})])
    def test_the_disclosure_follows_the_grant(self, rf, capability_on, expected):
        from accounts.context_processors import social_consent

        with (
            patch("codebase.conf.settings") as codebase_mock,
            patch("core.site_settings.site_settings") as settings_mock,
        ):
            codebase_mock.CLIENT = GitPlatform.GITLAB
            settings_mock.cross_project_access_enabled = capability_on
            assert social_consent(rf.get("/accounts/login/")) == expected

    def test_a_platform_without_oauth_says_nothing(self, rf):
        from accounts.context_processors import social_consent

        with (
            patch("codebase.conf.settings") as codebase_mock,
            patch("core.site_settings.site_settings") as settings_mock,
        ):
            codebase_mock.CLIENT = GitPlatform.SWE
            settings_mock.cross_project_access_enabled = True
            assert social_consent(rf.get("/accounts/login/")) == {}
