import uuid

from django.test import Client
from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.mark.django_db
class TestDashboardChatSegment:
    """Dashboard breakdown bar must include a Chat tile (not silently absorb it into Other)."""

    def _make_run(self, user, trigger_type, status=RunStatus.SUCCESSFUL):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="daiv/test", user=user
        )
        return Run.objects.create(
            session=session, status=status, trigger_type=trigger_type, repo_id="daiv/test", user=user
        )

    def test_chat_segment_appears_with_correct_count_and_url(self, user):
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.API_JOB)

        client = Client()
        client.force_login(user)
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        labels = [s["label"] for s in segments]
        assert "Chat" in labels

        chat_seg = next(s for s in segments if s["label"] == "Chat")
        assert chat_seg["value"] == 2
        sessions_url = reverse("session_list")
        assert chat_seg["url"] == f"{sessions_url}?trigger={SessionOrigin.CHAT}"

    def test_chat_runs_not_double_counted_in_other(self, user):
        # 1 UI_JOB run → goes to Other (not a named trigger segment)
        # 2 CHAT runs → go to Chat segment, NOT Other
        self._make_run(user, SessionOrigin.UI_JOB)
        self._make_run(user, SessionOrigin.CHAT)
        self._make_run(user, SessionOrigin.CHAT)

        client = Client()
        client.force_login(user)
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200

        segments = response.context["activity"]["segments"]
        seg_by_label = {s["label"]: s["value"] for s in segments}

        # Chat counts correctly
        assert seg_by_label.get("Chat", 0) == 2
        # Other gets only the UI_JOB run (1), not the chat runs
        assert seg_by_label.get("Other", 0) == 1
