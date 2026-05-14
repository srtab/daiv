from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import User


@pytest.mark.django_db
class TestPickerUsersView:
    def test_requires_authentication(self):
        client = Client()
        response = client.get(reverse("picker_users"), {"q": "ali"})
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_returns_empty_fragment_for_short_query(self, member_client):
        response = member_client.get(reverse("picker_users"), {"q": "a"})
        assert response.status_code == 200
        assert "No users found." in response.content.decode()

    def test_whitespace_only_query_is_treated_as_empty(self, member_client):
        User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": " a "})
        assert "alice" not in response.content.decode()

    def test_two_char_query_returns_matches(self, member_client):
        User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "al"})
        assert "alice" in response.content.decode()

    def test_matches_by_username(self, member_client):
        User.objects.create_user(username="alice", email="alice@t.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "ali"})
        html = response.content.decode()
        assert "alice" in html
        assert "alice@t.com" in html

    def test_matches_by_email(self, member_client):
        User.objects.create_user(username="alice", email="bob@company.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "bob@com"})
        assert "alice" in response.content.decode()

    def test_matches_by_name(self, member_client):
        User.objects.create_user(username="alice", email="a@t.com", password="x", name="Alice Doe")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "Doe"})
        assert "Alice Doe" in response.content.decode()

    def test_excludes_requesting_user(self, member_client, member_user):
        response = member_client.get(reverse("picker_users"), {"q": member_user.username[:3]})
        assert member_user.username not in response.content.decode()

    def test_honors_explicit_exclude_list(self, member_client):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        User.objects.create_user(username="alicia", email="b@t.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "ali", "exclude": str(alice.pk)})
        html = response.content.decode()
        assert "alicia" in html
        # alice (excluded) should not appear as a click target
        assert f"id: {alice.pk}," not in html

    def test_ignores_non_numeric_exclude_entries(self, member_client):
        User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "ali", "exclude": "foo,,bar"})
        assert "alice" in response.content.decode()

    def test_excludes_inactive_users(self, member_client):
        u = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        u.is_active = False
        u.save()
        response = member_client.get(reverse("picker_users"), {"q": "ali"})
        assert "alice" not in response.content.decode()

    def test_emits_click_handler_with_user_payload(self, member_client):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x", name="Alice Doe")  # noqa: S106
        response = member_client.get(reverse("picker_users"), {"q": "ali"})
        html = response.content.decode()
        assert f"addUser({{ id: {alice.pk}, username: 'alice', name: 'Alice Doe', email: 'a@t.com' }})" in html

    def test_caps_results_at_picker_limit(self, member_client):
        from accounts.views import PICKER_USERS_LIMIT

        User.objects.bulk_create([
            User(username=f"alice{i:02d}", email=f"a{i}@t.com", is_active=True) for i in range(PICKER_USERS_LIMIT + 2)
        ])
        response = member_client.get(reverse("picker_users"), {"q": "ali"})
        # One <li> with a click handler per rendered match; cap is enforced before render.
        assert response.content.decode().count("addUser(") == PICKER_USERS_LIMIT

    def test_orders_matches_by_username(self, member_client):
        User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        User.objects.create_user(username="alex", email="b@t.com", password="x")  # noqa: S106
        User.objects.create_user(username="albert", email="c@t.com", password="x")  # noqa: S106
        html = member_client.get(reverse("picker_users"), {"q": "al"}).content.decode()
        assert html.index("albert") < html.index("alex") < html.index("alice")
