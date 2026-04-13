from django.urls import reverse

import pytest


@pytest.mark.django_db
class TestScheduleCreateView:
    def test_form_renders_notification_fields(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "notify_on" in content
        assert "notify_channels" in content
