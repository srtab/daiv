import uuid
from datetime import datetime, time, timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin


def _local_noon(days_ago: int) -> datetime:
    return timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days_ago), time(12)))


def _make_run(user, trigger_type):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="daiv/test", user=user)
    return Run.objects.create(
        session=session, status=RunStatus.SUCCESSFUL, trigger_type=trigger_type, repo_id="daiv/test", user=user
    )


@pytest.mark.django_db
class TestHomepage:
    def test_anonymous_sees_the_homepage(self):
        response = Client().get(reverse("homepage"))
        assert response.status_code == 200
        assert "dashboard/homepage.html" in [t.name for t in response.templates]

    def test_signed_in_user_is_sent_to_the_dashboard(self, member_client):
        response = member_client.get(reverse("homepage"))
        assert response.status_code == 302
        assert response.url == reverse("dashboard")


@pytest.mark.django_db
class TestDashboardPeriod:
    def test_seven_days_covers_today_and_the_six_days_before(self, member_user, member_client):
        inside = _make_run(member_user, SessionOrigin.API_JOB)
        outside = _make_run(member_user, SessionOrigin.API_JOB)
        Run.objects.filter(pk=inside.pk).update(created_at=_local_noon(6))
        Run.objects.filter(pk=outside.pk).update(created_at=_local_noon(7))

        response = member_client.get(reverse("dashboard"), {"period": "7d"})

        assert response.context["activity"]["total"] == 1


@pytest.mark.django_db
class TestDashboardChatSegment:
    """Dashboard breakdown bar must include a Chat tile (not silently absorb it into Other)."""

    def test_chat_segment_appears_with_correct_count_and_url(self, member_user, member_client):
        _make_run(member_user, SessionOrigin.CHAT)
        _make_run(member_user, SessionOrigin.CHAT)
        _make_run(member_user, SessionOrigin.API_JOB)

        response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        labels = [s["label"] for s in segments]
        assert "Chat" in labels

        chat_seg = next(s for s in segments if s["label"] == "Chat")
        assert chat_seg["value"] == 2
        sessions_url = reverse("session_list")
        assert chat_seg["url"] == f"{sessions_url}?trigger={SessionOrigin.CHAT}"

    def test_chat_runs_not_double_counted_in_other(self, member_user, member_client):
        # 1 UI_JOB run → goes to Other (not a named trigger segment)
        # 2 CHAT runs → go to Chat segment, NOT Other
        _make_run(member_user, SessionOrigin.UI_JOB)
        _make_run(member_user, SessionOrigin.CHAT)
        _make_run(member_user, SessionOrigin.CHAT)

        response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        seg_by_label = {s["label"]: s["value"] for s in segments}

        # Chat counts correctly
        assert seg_by_label.get("Chat", 0) == 2
        # Other gets only the UI_JOB run (1), not the chat runs
        assert seg_by_label.get("Other", 0) == 1
