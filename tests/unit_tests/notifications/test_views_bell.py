from django.urls import reverse

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


@pytest.mark.django_db
class TestBellMarkup:
    """The dropdown used to close roughly ten seconds after it was opened.

    The bell polled itself with `hx-get` + `hx-swap="outerHTML"` on the element that
    owned the Alpine `open` flag, so each badge refresh replaced the component holding
    the dropdown's state. The badge now reads from the `nav` store — no swap, nothing
    to reset — so these assertions guard the regression rather than the markup taste.
    """

    def test_the_bell_does_not_re_render_itself(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        bell = content.split('id="notifications-bell"', 1)[1].split("</button>", 1)[0]
        assert 'hx-trigger="every' not in bell
        assert 'hx-swap="outerHTML"' not in bell

    def test_the_badge_is_bound_to_the_store(self, member_client, member_user):
        Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'x-text="$store.nav.unread"' in content
        # Seeded server-side so the badge does not flash in before the stream connects.
        assert "unread: 1" in content

    def test_the_dropdown_is_still_fetched_on_open_not_on_a_timer(self, member_client):
        """Lazy-loading the rows is a user action, not polling — it stays."""
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'hx-trigger="load-dropdown"' in content
