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

    def test_marks_all_unread_as_read_on_open(self, member_client, member_user):
        for i in range(15):
            Notification.objects.create(
                recipient=member_user, event_type="schedule.finished", subject=f"n{i}", body="b", link_url="/"
            )
        response = member_client.get("/dashboard/notifications/bell/")
        assert response.status_code == 200
        assert Notification.objects.filter(recipient=member_user, read_at__isnull=True).count() == 0

    def test_visible_rows_keep_unread_cue_on_first_open(self, member_client, member_user):
        Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        response = member_client.get("/dashboard/notifications/bell/")
        # Pins the fetch-before-update ordering — if the bulk update ran first, the green
        # dot (bg-emerald-400) would be absent here.
        assert "bg-emerald-400" in response.content.decode()

    def test_does_not_touch_other_users_notifications(self, member_client, admin_user):
        other = Notification.objects.create(
            recipient=admin_user, event_type="schedule.finished", subject="other", body="b", link_url="/"
        )
        member_client.get("/dashboard/notifications/bell/")
        other.refresh_from_db()
        assert other.read_at is None
