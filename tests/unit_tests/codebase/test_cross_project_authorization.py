"""The permission guarantee, tested at DAIV's own seam.

User Story 2 is a single claim: a person never sees more than they are entitled to. These tests
pin the three ways that claim could quietly stop being true — a fallback retry under the service
identity, target-project content surviving a denial, and a person's token reaching agent state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from django.core.cache import cache

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from accounts.credentials import CredentialReason, ResolvedCredential
from accounts.models import User
from automation.agent.middlewares.git_platform import _run_github_subcommand, _run_gitlab_subcommand
from codebase.base import GitPlatform
from codebase.models import CrossProjectAccessRecord

pytestmark = pytest.mark.django_db(transaction=True)

ATTACHED = "group/repo"
DENIED = "secret-group/secret-repo"
LARGE_TOOL_RESULTS_PREFIX = "/workspace/large_tool_results"

# What a denial must never surface. Stands in for the target project's issues, code and names.
TARGET_CONTENT = "ACQUISITION-CODENAME-BLUEBIRD"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(db):
    return User.objects.create_user(username="ada", email="ada@test.com", password="pw")  # noqa: S106


def _runtime(platform: GitPlatform, acting_user_id: int | None):
    return ToolRuntime(
        state={},
        context=Mock(
            repository=Mock(slug=ATTACHED),
            git_platform=platform,
            acting_user_id=acting_user_id,
            acting_platform_uid=None,
        ),
        config={"configurable": {"thread_id": "thread-under-test"}},
        stream_writer=Mock(),
        tool_call_id="call-1",
        store=None,
    )


def _backend():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=Mock(error=None))
    return backend


def _gitlab_settings():
    settings = Mock()
    settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "service-token"  # noqa: S106
    settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    return settings


async def _run_gitlab(
    runtime,
    *,
    project,
    resolved=None,
    returncode=0,
    stdout=b"ok\n",
    stderr=b"",
    subcommand="project-issue list",
    side_effect=None,
    cross_project_enabled=True,
):
    """``(result, create_proc, resolve)``. ``resolved=None`` leaves the resolve mock unconfigured,
    for the tests asserting no credential was spent."""
    proc = Mock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", _gitlab_settings()),
        patch("automation.agent.middlewares.git_platform.aresolve_access_token") as resolve,
        patch("automation.agent.middlewares.git_platform.ainvalidate_cached_token", AsyncMock(return_value=None)),
    ):
        create_proc.return_value = proc
        if resolved is not None:
            resolve.return_value = resolved
        if side_effect is not None:
            create_proc.side_effect = side_effect
        result = await _run_gitlab_subcommand(
            subcommand,
            runtime,
            "simplified",
            False,
            backend=_backend(),
            large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
            project=project,
            cross_project_enabled=cross_project_enabled,
        )
    return result, create_proc, resolve


class TestNoFallbackToTheServiceIdentity:
    """T036 / FR-006 / SC-002 — the natural "fall back so the agent gets an answer" instinct is
    precisely the leak this forbids."""

    @pytest.mark.parametrize(
        "reason",
        [
            CredentialReason.NO_CREDENTIAL,
            CredentialReason.EXPIRED,
            CredentialReason.REVOKED,
            CredentialReason.INSUFFICIENT_SCOPE,
            CredentialReason.NO_ACTING_USER,
            CredentialReason.DISABLED,
        ],
    )
    async def test_a_credential_denial_spawns_no_process_at_all(self, person, reason):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, _ = await _run_gitlab(runtime, project=DENIED, resolved=ResolvedCredential(reason=reason))

        assert result.startswith("error: ")
        create_proc.assert_not_called()

        records = [r async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert len(records) == 1
        assert records[0].outcome != CrossProjectAccessRecord.Outcome.ALLOWED
        # The whole point: the row names the person the run acts for, never DAIV itself, and the
        # attribution survives the person's deletion.
        assert records[0].acting_user_id == person.pk
        assert records[0].acting_user_label

    async def test_a_platform_denial_is_not_retried_under_the_service_token(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stderr=b"404 Project Not Found",
        )

        assert result.startswith("error: ")
        # Exactly one attempt, and it carried the person's token — never a second under the
        # deployment's own.
        assert create_proc.call_count == 1
        assert create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "person-token"  # noqa: S105

        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_NO_ACCESS]
        # One row, attributed to the person — nothing recorded a second, service-identity attempt.
        assert (
            await CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED, acting_user_id=person.pk).acount() == 1
        )


class TestNoTargetContentSurvivesADenial:
    """T037 / FR-005 — nothing from the refused project may appear in the answer, the audit row,
    or a state update."""

    async def test_denied_content_is_absent_from_the_result_and_the_record(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, _, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stdout=TARGET_CONTENT.encode(),
            stderr=f"403 Forbidden while reading {TARGET_CONTENT}".encode(),
        )

        assert TARGET_CONTENT not in result
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id=DENIED)
        # The record proves *that* a project was reached, never *what* was in it.
        assert TARGET_CONTENT not in str(record.__dict__)

    async def test_a_denial_returns_a_plain_string_not_a_state_update(self, person):
        runtime = _runtime(GitPlatform.GITHUB, person.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(TARGET_CONTENT.encode(), b"404 Not Found"))
        proc.returncode = 1
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch("automation.agent.middlewares.git_platform.ainvalidate_cached_token", AsyncMock(return_value=None)),
        ):
            create_proc.return_value = proc
            result = await _run_github_subcommand(
                "issue list",
                runtime,
                False,
                backend=_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=DENIED,
                cross_project_enabled=True,
            )

        assert not isinstance(result, Command)
        assert TARGET_CONTENT not in result
        assert runtime.state == {}


class TestThePersonsTokenNeverEntersState:
    """T038 / FR-012 / D6 — agent state is checkpointed, so a person's token in it is a token at
    rest in Redis under a key nobody revokes."""

    async def test_a_successful_cross_project_github_call_produces_no_command(self, person):
        runtime = _runtime(GitPlatform.GITHUB, person.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        proc.returncode = 0
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch("automation.agent.middlewares.git_platform._get_cached_github_cli_token") as cached_mock,
        ):
            create_proc.return_value = proc
            result = await _run_github_subcommand(
                "issue list",
                runtime,
                False,
                backend=_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="other/repo",
                cross_project_enabled=True,
            )

        assert result == "ok"
        assert not isinstance(result, Command)
        assert runtime.state == {}
        # The installation-token cache is DAIV's own path; a cross-project call must not touch it.
        cached_mock.assert_not_called()

    async def test_an_allowed_cross_project_call_is_recorded_without_the_token(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        await _run_gitlab(runtime, project="other/repo", resolved=ResolvedCredential(token="person-token"))  # noqa: S106

        record = await CrossProjectAccessRecord.objects.aget(target_repo_id="other/repo")
        assert record.outcome == CrossProjectAccessRecord.Outcome.ALLOWED
        assert record.acting_user_id == person.pk
        assert record.thread_id == "thread-under-test"
        assert "person-token" not in str(record.__dict__)


class TestNoTokenReachesALogRecord:
    """T061 / FR-012 — exception messages are where secrets usually escape."""

    async def test_a_failed_cross_project_call_logs_no_token(self, person, caplog):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        with caplog.at_level("DEBUG"):
            result, _, _ = await _run_gitlab(
                runtime,
                project=DENIED,
                resolved=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
                returncode=1,
                stderr=b"remote: HTTP Basic: Access denied for glpat-PERSONTOKEN",
            )

        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text

    async def test_a_subprocess_failure_logs_no_token(self, person, caplog):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        with (
            caplog.at_level("DEBUG"),
            patch(
                "automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec",
                side_effect=OSError("spawn failed"),
            ),
            patch("automation.agent.middlewares.git_platform.settings", _gitlab_settings()),
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
            ),
        ):
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=DENIED,
                cross_project_enabled=True,
            )

        assert result.startswith("error: ")
        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text

    async def test_a_failed_refresh_logs_no_token(self, person, caplog):
        """The token endpoint's error body routinely echoes the refresh token."""
        from accounts import credentials

        credential = Mock(user_id=person.pk, provider="gitlab", host="gitlab.com", pk=1, scopes=["api"])
        response = Mock(status_code=400)
        response.json.return_value = {"error": "invalid_grant", "refresh_token": "rt-SECRET"}

        with (
            caplog.at_level("DEBUG"),
            patch("accounts.credentials.httpx.AsyncClient") as client_cls,
            patch("accounts.credentials.site_settings") as settings_mock,
        ):
            settings_mock.auth_client_id = "cid"
            settings_mock.auth_client_secret = "secret"  # noqa: S105
            settings_mock.auth_gitlab_url = "https://gitlab.com"
            settings_mock.auth_gitlab_server_url = None
            client = client_cls.return_value.__aenter__.return_value
            client.post = AsyncMock(return_value=response)

            payload = await credentials._arequest_refresh(credential, "rt-SECRET")

        assert payload is credentials.RefreshFailure.TERMINAL
        assert "rt-SECRET" not in caplog.text


class TestEveryAttemptLeavesARow:
    """FR-016 — a pattern of refusals is what an auditor is looking for, so the attempts that
    spend the token and then fail are the ones most worth a row."""

    async def test_a_timeout_is_recorded(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, _, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=TimeoutError,
        )

        assert "timed out" in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_failure_to_launch_is_recorded(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, _, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=OSError("spawn failed"),
        )

        assert result.startswith("error: ")
        assert "spawn failed" not in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_disallowed_subcommand_is_recorded(self, person):
        """The allow-list check has to run before the target decision, so its refusals would
        otherwise be the one cross-project attempt with no row."""
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand="project-hook create --url http://evil.test",
        )

        assert "not allowed by policy" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_capability_being_off_is_recorded_as_such(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, _ = await _run_gitlab(runtime, project=DENIED, cross_project_enabled=False)

        assert "not enabled" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_DISABLED]


class TestPolicyRefusalsSpendNothing:
    """A denied verb is a static table lookup; resolving a credential first would rotate a GitLab
    refresh token for a call that can never run."""

    @pytest.mark.parametrize(
        ("subcommand", "why"),
        [
            ("project-branch create --branch x --ref main", "denied verb"),
            ("project-issue move --iid 1 --to-project-id 9", "relocates an issue out of the project"),
            ("project-issue update --iid 1 --state-event close", "closes through an allowed verb"),
            ("project-merge-request update --iid 1 --target-branch other", "repoints an MR"),
            ("project-merge-request-note update --mr-iid 1 --note-id 2 --body x", "edits someone else's note"),
            ("project-label create --name x --color '#fff'", "changes project configuration"),
        ],
    )
    async def test_a_policy_refusal_resolves_no_credential(self, person, subcommand, why):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, resolve = await _run_gitlab(runtime, project=DENIED, subcommand=subcommand)

        assert result.startswith("error: "), why
        create_proc.assert_not_called()
        resolve.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_same_verbs_still_work_on_the_attached_project(self, person):
        """Everything above stays available under DAIV's own identity where the run is attached."""
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, _ = await _run_gitlab(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand="project-issue update --iid 1 --state-event close",
        )

        assert not result.startswith("error: ")
        assert create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert not await CrossProjectAccessRecord.objects.aexists()


class TestNoUnattributedCrossProjectWrite:
    """FR-015 — a cross-project write carries a person's attribution, so the webhook's own
    "is this mine?" check cannot recognise it. The marker is the only thing that can."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue-note create --issue-iid 1 --body "hello"',
            'project-issue-note create --body="hello" --issue-iid 1',
            'project-issue create --title t --description "hello"',
            'project-issue create --title t --description="hello"',
        ],
    )
    async def test_a_published_body_carries_the_marker(self, person, subcommand):
        from core.constants import CROSS_PROJECT_CONTENT_MARKER

        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        _, create_proc, _ = await _run_gitlab(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand=subcommand,
        )

        argv = create_proc.call_args.args
        assert any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_the_attached_project_body_is_left_alone(self, person):
        from core.constants import CROSS_PROJECT_CONTENT_MARKER

        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        _, create_proc, _ = await _run_gitlab(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand='project-issue-note create --issue-iid 1 --body "hello"',
        )

        argv = create_proc.call_args.args
        assert not any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_a_body_read_from_a_file_is_refused_rather_than_published_unmarked(self, person):
        runtime = _runtime(GitPlatform.GITLAB, person.pk)
        result, create_proc, resolve = await _run_gitlab(
            runtime,
            project=DENIED,
            subcommand="project-issue create --title t --body-file /tmp/note.md",  # noqa: S108
        )

        assert result.startswith("error: ")
        create_proc.assert_not_called()
        # Decided by the policy gate, so it costs no GitLab refresh-token rotation.
        resolve.assert_not_called()
