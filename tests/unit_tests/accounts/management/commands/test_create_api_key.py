from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from accounts.models import APIKey, User


@pytest.mark.django_db
class TestCreateApiKey:
    @pytest.fixture
    def user(self):
        return User.objects.create_user(username="testuser", email="test@example.com", password="testpass")  # noqa: S106

    def test_creates_api_key(self, user):
        call_command("create_api_key", user.username, name="test-key")

        assert APIKey.objects.count() == 1
        api_key = APIKey.objects.first()
        assert api_key.user == user
        assert api_key.name == "test-key"
        assert api_key.expires_at is None
        assert api_key.revoked is False

    def test_creates_api_key_with_expiration(self, user):
        call_command("create_api_key", user.username, name="expiring", expires_at="2030-06-15T12:00:00")

        api_key = APIKey.objects.first()
        assert api_key.expires_at is not None
        assert api_key.expires_at.year == 2030
        assert api_key.expires_at.month == 6
        assert api_key.expires_at.day == 15

    def test_creates_api_key_with_empty_name(self, user):
        call_command("create_api_key", user.username)

        api_key = APIKey.objects.first()
        assert api_key.name == ""

    def test_created_key_is_verifiable(self, user, capsys):
        call_command("create_api_key", user.username, name="verify-test")

        captured = capsys.readouterr()
        printed_key = captured.out.splitlines()[0].removeprefix("API key created: ")

        api_key = APIKey.objects.first()
        assert APIKey.objects.key_generator.verify(printed_key, api_key.hashed_key)

    def test_nonexistent_user_raises_error(self):
        with pytest.raises(CommandError, match="does not exist"):
            call_command("create_api_key", "nonexistent")

    def test_invalid_date_raises_error(self, user):
        with pytest.raises(CommandError, match="Invalid date format"):
            call_command("create_api_key", user.username, expires_at="not-a-date")

    def test_prints_key_to_stdout(self, user, capsys):
        call_command("create_api_key", user.username, name="stdout-test")

        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert lines[0].startswith("API key created: ")
        assert "Store this key securely" in lines[1]
