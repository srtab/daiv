import pytest

from accounts.forms import UserCreateForm, UserUpdateForm
from accounts.models import Role, User


@pytest.mark.django_db
class TestUserCreateForm:
    def test_valid_form(self):
        form = UserCreateForm(data={"name": "Alice", "email": "alice@test.com", "role": Role.MEMBER})
        assert form.is_valid()

    def test_email_required(self):
        form = UserCreateForm(data={"name": "Alice", "email": "", "role": Role.MEMBER})
        assert not form.is_valid()
        assert "email" in form.errors

    def test_duplicate_email(self, admin_user):
        form = UserCreateForm(data={"name": "Duplicate", "email": admin_user.email, "role": Role.MEMBER})
        assert not form.is_valid()
        assert "email" in form.errors

    def test_save_generates_username(self):
        form = UserCreateForm(data={"name": "Bob", "email": "bob@test.com", "role": Role.MEMBER})
        assert form.is_valid()
        user = form.save()
        assert user.username.startswith("user_")
        assert len(user.username) == 17  # "user_" + 12 hex chars

    def test_save_sets_unusable_password(self):
        form = UserCreateForm(data={"name": "Carol", "email": "carol@test.com", "role": Role.MEMBER})
        assert form.is_valid()
        user = form.save()
        assert not user.has_usable_password()

    def test_role_defaults_to_member(self):
        form = UserCreateForm(data={"name": "Dave", "email": "dave@test.com", "role": Role.MEMBER})
        assert form.is_valid()
        user = form.save()
        assert user.role == Role.MEMBER


@pytest.mark.django_db
class TestUserUpdateForm:
    def test_valid_update(self, member_user, admin_user):
        form = UserUpdateForm(
            data={"name": "Updated", "email": member_user.email, "role": Role.MEMBER, "is_active": True},
            instance=member_user,
            requesting_user=admin_user,
        )
        assert form.is_valid()

    def test_cannot_deactivate_self(self, admin_user):
        form = UserUpdateForm(
            data={"name": admin_user.name, "email": admin_user.email, "role": Role.ADMIN, "is_active": False},
            instance=admin_user,
            requesting_user=admin_user,
        )
        assert not form.is_valid()
        assert "is_active" in form.errors

    def test_cannot_demote_self_as_last_admin(self, admin_user):
        form = UserUpdateForm(
            data={"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": True},
            instance=admin_user,
            requesting_user=admin_user,
        )
        assert not form.is_valid()
        assert "role" in form.errors

    def test_can_demote_self_when_other_admin_exists(self, admin_user):
        User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        form = UserUpdateForm(
            data={"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": True},
            instance=admin_user,
            requesting_user=admin_user,
        )
        assert form.is_valid()

    def test_cannot_demote_last_admin_even_as_other_admin(self, admin_user):
        other_admin = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",  # noqa: S106
            role=Role.ADMIN,
        )
        # Demote other_admin so admin_user becomes the last admin
        other_admin.role = Role.MEMBER
        other_admin.save()

        form = UserUpdateForm(
            data={"name": admin_user.name, "email": admin_user.email, "role": Role.MEMBER, "is_active": True},
            instance=admin_user,
            requesting_user=other_admin,
        )
        assert not form.is_valid()
        assert "role" in form.errors
