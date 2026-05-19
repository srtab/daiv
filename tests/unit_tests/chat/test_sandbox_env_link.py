import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from chat.models import ChatThread


@pytest.mark.django_db
def test_chat_thread_link(db):
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    thread = ChatThread.objects.create(thread_id="t1", user=user, repo_id="r/p", sandbox_environment=env)
    thread.refresh_from_db()
    assert thread.sandbox_environment_id == env.id
    env.delete()
    thread.refresh_from_db()
    assert thread.sandbox_environment_id is None
