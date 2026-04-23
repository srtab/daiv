import pytest

from accounts.filters import UserFilter
from accounts.models import Role, User


@pytest.fixture
def users(db):
    alice = User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
        name="Alice Admin",
        role=Role.ADMIN,
    )
    bob = User.objects.create_user(
        username="bob",
        email="bob@test.com",
        password="testpass123",  # noqa: S106
        name="Bob Member",
        role=Role.MEMBER,
    )
    return alice, bob


@pytest.mark.django_db
class TestUserFilter:
    def test_no_params_returns_all(self, users):
        alice, bob = users
        qs = UserFilter({}, queryset=User.objects.all()).qs
        assert alice in qs
        assert bob in qs

    def test_q_matches_name(self, users):
        alice, bob = users
        qs = UserFilter({"q": "Alice"}, queryset=User.objects.all()).qs
        assert alice in qs
        assert bob not in qs

    def test_q_matches_email(self, users):
        alice, bob = users
        qs = UserFilter({"q": "bob@"}, queryset=User.objects.all()).qs
        assert bob in qs
        assert alice not in qs

    def test_q_is_case_insensitive(self, users):
        alice, _bob = users
        qs = UserFilter({"q": "alice"}, queryset=User.objects.all()).qs
        assert alice in qs

    def test_q_whitespace_is_stripped(self, users):
        alice, bob = users
        qs = UserFilter({"q": "   "}, queryset=User.objects.all()).qs
        assert alice in qs
        assert bob in qs

    def test_role_filter_admin(self, users):
        alice, bob = users
        qs = UserFilter({"role": Role.ADMIN}, queryset=User.objects.all()).qs
        assert alice in qs
        assert bob not in qs

    def test_role_filter_member(self, users):
        alice, bob = users
        qs = UserFilter({"role": Role.MEMBER}, queryset=User.objects.all()).qs
        assert bob in qs
        assert alice not in qs

    def test_invalid_role_is_ignored(self, users):
        alice, bob = users
        f = UserFilter({"role": "bogus"}, queryset=User.objects.all())
        # Invalid choice → form invalid → FilterView fallback: no filter applied.
        assert not f.form.is_valid()
        # Still returns full queryset since the invalid value cannot be translated to a filter.
        assert alice in f.qs
        assert bob in f.qs

    def test_q_and_role_combined(self, users):
        alice, bob = users
        qs = UserFilter({"q": "bob", "role": Role.MEMBER}, queryset=User.objects.all()).qs
        assert bob in qs
        assert alice not in qs
