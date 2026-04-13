import pytest
from notifications.models import Notification


@pytest.mark.django_db
class TestBellDropdown:
    def test_renders_unread_count_and_recent(self, member_client, member_user):
        for i in range(3):
            Notification.objects.create(
                recipient=member_user, event_type="schedule.finished", subject=f"n{i}", body="b", link_url="/"
            )
        response = member_client.get("/dashboard/notifications/bell/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "n0" in content and "n2" in content

    def test_limits_to_ten_recent(self, member_client, member_user):
        for i in range(15):
            Notification.objects.create(
                recipient=member_user, event_type="schedule.finished", subject=f"msg{i}", body="b", link_url="/"
            )
        response = member_client.get("/dashboard/notifications/bell/")
        # Newest ten are msg5..msg14
        content = response.content.decode()
        assert "msg14" in content
        assert "msg4" not in content  # 11th-newest excluded
