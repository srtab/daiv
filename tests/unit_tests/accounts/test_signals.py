from unittest.mock import Mock, patch

import pytest
from allauth.account.signals import user_logged_in

from accounts.models import User


@pytest.fixture
def adapter():
    with patch("allauth.socialaccount.adapter.get_adapter") as get_adapter:
        yield get_adapter.return_value


def _log_in(sociallogin):
    user_logged_in.send(sender=User, request=Mock(), user=Mock(pk=7), sociallogin=sociallogin)


def test_a_social_login_hands_its_grant_to_the_adapter(adapter):
    sociallogin = Mock()

    _log_in(sociallogin)

    adapter.capture_platform_credential.assert_called_once_with(sociallogin)


def test_a_login_without_a_social_account_captures_nothing(adapter):
    _log_in(None)

    adapter.capture_platform_credential.assert_not_called()


def test_a_capture_failure_never_breaks_sign_in(adapter, caplog):
    adapter.capture_platform_credential.side_effect = RuntimeError("boom")

    _log_in(Mock())

    assert "Failed to capture the platform credential" in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_capture_failure_never_logs_the_failure_message_or_a_traceback(adapter, caplog):
    adapter.capture_platform_credential.side_effect = RuntimeError("tok-from-platform")

    _log_in(Mock())

    assert "tok-from-platform" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
