from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from accounts.models import Role, User


@pytest.mark.django_db
class TestBootstrapAdmin:
    def test_creates_admin_on_empty_system(self):
        call_command("bootstrap_admin", "admin@example.com")
        user = User.objects.get(email="admin@example.com")
        assert user.role == Role.ADMIN
        assert user.is_active

    def test_refuses_when_admin_exists(self):
        User.objects.create_user(username="existing-admin", email="admin@example.com", role=Role.ADMIN)
        with pytest.raises(CommandError, match="admin user already exists"):
            call_command("bootstrap_admin", "new@example.com")

    def test_refuses_when_email_collides(self):
        User.objects.create_user(username="member", email="taken@example.com", role=Role.MEMBER)
        with pytest.raises(CommandError, match="already exists"):
            call_command("bootstrap_admin", "taken@example.com")

    def test_prints_success_message(self, capsys):
        call_command("bootstrap_admin", "admin@example.com")
        captured = capsys.readouterr()
        assert "admin@example.com" in captured.out
        assert "login-by-code" in captured.out
