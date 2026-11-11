import uuid
from datetime import datetime, time, timedelta

from django.urls import reverse
from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin
from skills.models import SkillInvocation

USAGE_URL = reverse("skills:usage", args=["plan"])


def _invocation(thread_id, created, *, repo_slug="daiv/api") -> SkillInvocation:
    invocation = SkillInvocation.objects.create(
        name="plan", source=SkillInvocation.Source.BUILTIN, repo_slug=repo_slug, thread_id=thread_id
    )
    SkillInvocation.objects.filter(pk=invocation.pk).update(created=created)
    invocation.refresh_from_db()
    return invocation


def _session(thread_id) -> Session:
    return Session.objects.create(
        thread_id=str(thread_id), origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", title="Plan the importer"
    )


def _run(session, started_at, *, status=RunStatus.SUCCESSFUL) -> Run:
    return Run.objects.create(
        session=session,
        status=status,
        trigger_type=SessionOrigin.MCP_JOB,
        repo_id=session.repo_id,
        started_at=started_at,
    )


@pytest.mark.django_db
def test_the_tab_counts_the_invocations_of_the_last_30_days(admin_client):
    today = timezone.localdate()
    for offset in (0, 1, 1, 5, 31):
        _invocation(uuid.uuid4(), timezone.make_aware(datetime.combine(today - timedelta(days=offset), time(12))))

    usage = admin_client.get(USAGE_URL).context["usage"]

    assert usage["total"] == 5
    assert usage["last_30_total"] == 4
    counts = {entry["day"]: entry["count"] for entry in usage["daily_series"]}
    assert len(counts) == 30
    assert (counts[today], counts[today - timedelta(days=1)], counts[today - timedelta(days=5)]) == (1, 2, 1)


@pytest.mark.django_db
def test_each_recent_invocation_opens_the_run_it_happened_in(admin_client):
    thread_id = uuid.uuid4()
    session = _session(thread_id)
    start = timezone.now() - timedelta(hours=3)
    first = _run(session, start)
    second = _run(session, start + timedelta(hours=1), status=RunStatus.FAILED)
    _invocation(thread_id, start + timedelta(minutes=10))
    _invocation(thread_id, start + timedelta(hours=2))

    response = admin_client.get(USAGE_URL)

    session_url = reverse("session_detail", kwargs={"thread_id": session.thread_id})
    assert [row.url for row in response.context["recent_runs"]] == [
        f"{session_url}#run-{second.pk}",
        f"{session_url}#run-{first.pk}",
    ]
    assert f'href="{session_url}#run-{second.pk}"' in response.content.decode()


@pytest.mark.django_db
def test_a_session_whose_thread_id_is_stored_as_bare_hex_is_matched(admin_client):
    thread_id = uuid.uuid4()
    session = Session.objects.create(thread_id=thread_id.hex, origin=SessionOrigin.MCP_JOB, repo_id="daiv/api")
    start = timezone.now() - timedelta(hours=1)
    run = _run(session, start)
    _invocation(thread_id, start + timedelta(minutes=5))

    [row] = admin_client.get(USAGE_URL).context["recent_runs"]

    assert row.url == reverse("session_detail", kwargs={"thread_id": thread_id.hex}) + f"#run-{run.pk}"


@pytest.mark.django_db
def test_an_invocation_before_any_run_started_opens_its_session(admin_client):
    thread_id = uuid.uuid4()
    session = _session(thread_id)
    _run(session, timezone.now())
    _invocation(thread_id, timezone.now() - timedelta(hours=1))

    [row] = admin_client.get(USAGE_URL).context["recent_runs"]

    assert row.run is None
    assert row.url == reverse("session_detail", kwargs={"thread_id": session.thread_id})


@pytest.mark.django_db
def test_an_invocation_whose_session_is_gone_is_listed_without_a_link(admin_client):
    _invocation(uuid.uuid4(), timezone.now(), repo_slug="daiv/web")

    response = admin_client.get(USAGE_URL)

    [row] = response.context["recent_runs"]
    assert (row.url, row.repo_slug) == ("", "daiv/web")
    assert "Session no longer available" in response.content.decode()


@pytest.mark.django_db
def test_a_skill_no_run_invoked_says_so(admin_client):
    response = admin_client.get(USAGE_URL)

    assert response.context["recent_runs"] == []
    assert "No run has invoked this skill yet." in response.content.decode()


@pytest.mark.django_db
def test_the_tab_is_for_admins_only(client, member_user):
    client.force_login(member_user)

    assert client.get(USAGE_URL).status_code == 403
