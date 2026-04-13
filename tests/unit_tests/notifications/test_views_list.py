import pytest
from notifications.models import Notification


@pytest.fixture
def notification(member_user):
    return Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="Hi", body="Body", link_url="/x/"
    )


@pytest.mark.django_db
class TestNotificationListView:
    def test_requires_login(self, client):
        response = client.get("/dashboard/notifications/")
        assert response.status_code in (302, 401)

    def test_lists_own_notifications(self, member_client, notification):
        response = member_client.get("/dashboard/notifications/")
        assert response.status_code == 200
        assert "Hi" in response.content.decode()

    def test_does_not_list_others_notifications(self, member_client, admin_user):
        Notification.objects.create(
            recipient=admin_user, event_type="schedule.finished", subject="Secret", body="b", link_url="/"
        )
        response = member_client.get("/dashboard/notifications/")
        assert "Secret" not in response.content.decode()


@pytest.mark.django_db
class TestMarkAsRead:
    def test_marks_single_as_read(self, member_client, notification):
        response = member_client.post(f"/dashboard/notifications/{notification.id}/read/")
        assert response.status_code in (200, 204)
        notification.refresh_from_db()
        assert notification.read_at is not None

    def test_404_on_other_user_notification(self, member_client, admin_user):
        other = Notification.objects.create(
            recipient=admin_user, event_type="schedule.finished", subject="x", body="b", link_url="/"
        )
        response = member_client.post(f"/dashboard/notifications/{other.id}/read/")
        assert response.status_code == 404

    def test_mark_all_as_read(self, member_client, member_user):
        for i in range(3):
            Notification.objects.create(
                recipient=member_user, event_type="schedule.finished", subject=f"s{i}", body="b", link_url="/"
            )
        response = member_client.post("/dashboard/notifications/read-all/")
        assert response.status_code in (200, 204)
        assert Notification.objects.filter(recipient=member_user, read_at__isnull=True).count() == 0
