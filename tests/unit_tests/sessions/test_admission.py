from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

import pytest
from mcp_connectors.models import MCPServer
from sandbox_envs.models import SandboxEnvironment, Visibility
from sessions.admission import (
    InvalidMcpOverridesError,
    RejectedOverrideError,
    RepoAccessError,
    UnknownEnvironmentError,
    aadmit,
    aresolve_environment,
    aresolve_mcp_overrides,
    assert_repo_access,
    check_agent_override,
)
from sessions.models import Session, SessionOrigin
from sessions.services import RepoTarget

from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied
from core.site_settings import site_settings
from tests.unit_tests.mcp_connectors.helpers import only_servers

_DENIED = RepositoryAccessDenied(["a/b"])


@pytest.fixture
def global_default(db):
    SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).delete()
    return SandboxEnvironment.objects.create(
        scope=Visibility.GLOBAL, name="Default", base_image="python:3.14", is_default=True
    )


@pytest.fixture
def user_env(member_user):
    return SandboxEnvironment.objects.create(
        scope=Visibility.USER, user=member_user, name="mine", base_image="python:3.14", repo_ids=["a/b"]
    )


@pytest.mark.django_db
def test_check_agent_override_normalises_the_pair():
    assert check_agent_override(None, " high ") == ("", "high")


@pytest.mark.django_db
def test_check_agent_override_rejects_an_invalid_model_with_the_validator_message():
    with pytest.raises(RejectedOverrideError, match="Unknown provider prefix 'nopesuch'"):
        check_agent_override("nopesuch:model-x", "")


@pytest.mark.django_db
def test_check_agent_override_rejects_a_request_no_model_can_serve():
    with (
        patch.multiple(site_settings, agent_model_name=""),
        pytest.raises(RejectedOverrideError, match="no system default"),
    ):
        check_agent_override("", "")


def test_assert_repo_access_hides_which_repository_was_denied():
    with (
        patch("sessions.admission.assert_can_run", new=Mock(side_effect=_DENIED)),
        pytest.raises(RepoAccessError) as err,
    ):
        assert_repo_access(Mock(), ["a/b"])
    assert str(err.value) == REPO_ACCESS_DENIED_MESSAGE
    assert err.value.repo_ids == ["a/b"]


@pytest.mark.django_db(transaction=True)
async def test_aresolve_environment_rejects_an_unknown_name(member_user):
    with pytest.raises(UnknownEnvironmentError, match="unknown environment 'nope'"):
        await aresolve_environment(member_user, "nope")


async def test_aresolve_environment_returns_none_when_nothing_is_named():
    assert await aresolve_environment(Mock(), "") is None


@pytest.mark.django_db(transaction=True)
async def test_aadmit_checks_the_override_before_repository_access(member_user):
    can_run = Mock(side_effect=_DENIED)
    with patch("sessions.admission.assert_can_run", new=can_run), pytest.raises(RejectedOverrideError):
        await aadmit(
            user=member_user,
            repos=[RepoTarget(repo_id="a/b")],
            agent_model="nopesuch:model-x",
            agent_thinking_level="",
            environment=None,
        )
    can_run.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_aadmit_checks_repository_access_before_the_environment(member_user):
    with patch("sessions.admission.assert_can_run", new=Mock(side_effect=_DENIED)), pytest.raises(RepoAccessError):
        await aadmit(
            user=member_user,
            repos=[RepoTarget(repo_id="a/b")],
            agent_model="",
            agent_thinking_level="",
            environment="nope",
        )


@pytest.mark.django_db(transaction=True)
async def test_aadmit_gives_every_target_the_named_environment(member_user, user_env):
    admission = await aadmit(
        user=member_user,
        repos=[RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d", ref="dev")],
        agent_model="",
        agent_thinking_level="low",
        environment="mine",
    )

    assert (admission.agent_model, admission.agent_thinking_level) == ("", "low")
    assert admission.targets == [
        RepoTarget(repo_id="a/b", sandbox_environment_id=str(user_env.id)),
        RepoTarget(repo_id="c/d", ref="dev", sandbox_environment_id=str(user_env.id)),
    ]


@pytest.mark.django_db(transaction=True)
async def test_aadmit_resolves_each_target_when_no_environment_is_named(member_user, user_env, global_default):
    admission = await aadmit(
        user=member_user,
        repos=[RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d")],
        agent_model=None,
        agent_thinking_level=None,
        environment=None,
    )

    assert [t.sandbox_environment_id for t in admission.targets] == [str(user_env.id), str(global_default.id)]


@pytest.fixture
def mcp_pool(db):
    only_servers(("rt", MCPServer.Status.ACTIVE), ("grafana", MCPServer.Status.ON_DEMAND))


@pytest.mark.django_db(transaction=True)
async def test_aresolve_mcp_overrides_resolves_against_the_callers_pool(member_user, mcp_pool):
    assert await aresolve_mcp_overrides(member_user, {"grafana": True, "rt": True}) == {"grafana": "on"}


@pytest.mark.django_db(transaction=True)
async def test_aresolve_mcp_overrides_refuses_an_unknown_server(member_user, mcp_pool):
    with pytest.raises(InvalidMcpOverridesError, match="grafna"):
        await aresolve_mcp_overrides(member_user, {"grafna": True})


@pytest.mark.django_db(transaction=True)
async def test_aresolve_mcp_overrides_applies_the_switches_on_top_of_the_threads(member_user, mcp_pool):
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, repo_id="a/b", mcp_overrides={"rt": "off"}
    )
    overrides = await aresolve_mcp_overrides(member_user, {"grafana": True}, session.thread_id)
    assert overrides == {"rt": "off", "grafana": "on"}


@pytest.mark.parametrize("switches", [None, {}])
async def test_aresolve_mcp_overrides_skips_the_pool_when_nothing_is_switched(switches):
    assert await aresolve_mcp_overrides(Mock(), switches) is None
