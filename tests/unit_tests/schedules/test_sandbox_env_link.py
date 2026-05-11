import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
def test_scheduled_job_link(db):
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    job = ScheduledJob.objects.create(
        user=user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "r/p", "ref": ""}],
        frequency=Frequency.DAILY,
        time="09:00",
        sandbox_environment=env,
    )
    job.refresh_from_db()
    assert job.sandbox_environment_id == env.id
    env.delete()
    job.refresh_from_db()
    assert job.sandbox_environment_id is None
