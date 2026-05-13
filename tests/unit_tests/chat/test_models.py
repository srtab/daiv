from django.db import IntegrityError

import pytest
from activity.models import Activity, TriggerType

from chat.models import ChatThread


@pytest.mark.django_db
def test_chat_thread_thread_id_is_unique_primary_key(member_user):
    ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", ref="main")
    with pytest.raises(IntegrityError):
        ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", ref="main")


@pytest.mark.django_db(transaction=True)
async def test_aget_or_create_from_activity_is_idempotent(member_user):
    activity = await Activity.objects.acreate(
        trigger_type=TriggerType.UI_JOB,
        repo_id="a/b",
        ref="main",
        prompt="first message",
        thread_id="t-42",
        user=member_user,
    )
    thread_a, created_a = await ChatThread.aget_or_create_from_activity(member_user, activity)
    thread_b, created_b = await ChatThread.aget_or_create_from_activity(member_user, activity)
    assert created_a is True
    assert created_b is False
    assert thread_a.thread_id == thread_b.thread_id == "t-42"
    assert thread_a.repo_id == "a/b"
    assert thread_a.ref == "main"
    assert thread_a.title.startswith("first message")
