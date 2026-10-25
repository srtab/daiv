from unittest.mock import Mock, patch

import pytest
from allauth.account.signals import user_logged_in

from accounts.credentials import REAUTHORISE_SESSION_KEY
from accounts.models import User


@pytest.fixture
def adapter():
    with patch("allauth.socialaccount.adapter.get_adapter") as get_adapter:
        yield get_adapter.return_value


def _log_in(sociallogin, session=None):
    request = Mock(session=session if session is not None else {})
    user_logged_in.send(sender=User, request=request, user=Mock(pk=7), sociallogin=sociallogin)


def test_a_social_login_hands_its_grant_to_the_adapter(adapter):
    sociallogin = Mock()

    _log_in(sociallogin)

    adapter.capture_platform_credential.assert_called_once_with(sociallogin, reauthorise=False)


@pytest.mark.parametrize(("requested_by", "reauthorise"), [(7, True), (8, False)])
def test_only_the_person_who_pressed_authorise_re_authorises(adapter, requested_by, reauthorise):
    session = {REAUTHORISE_SESSION_KEY: requested_by}

    _log_in(Mock(), session=session)

    assert adapter.capture_platform_credential.call_args.kwargs == {"reauthorise": reauthorise}
    assert REAUTHORISE_SESSION_KEY not in session


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
