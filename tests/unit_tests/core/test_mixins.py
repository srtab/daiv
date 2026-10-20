from django.test import Client
from django.urls import reverse

import pytest


@pytest.mark.django_db
class TestAdminRequiredMixin:
    def test_admin_can_access(self, admin_user):
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("user_list"))
        assert response.status_code == 200

    def test_member_gets_403(self, member_user):
        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("user_list"))
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self):
        client = Client()
        response = client.get(reverse("user_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url
