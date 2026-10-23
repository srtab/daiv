from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langgraph.types import Command

from accounts.credentials import CredentialReason, ResolvedCredential, aresolve_access_token
from automation.agent.middlewares.git_platform.cross_project import (
    REFUSAL_BODY_FILE_CROSS_PROJECT,
    REFUSAL_BOT_LABEL_CROSS_PROJECT,
    REFUSAL_CREDENTIAL_REJECTED,
    REFUSAL_DESTRUCTIVE_CROSS_PROJECT,
    REFUSAL_DISABLED,
    REFUSAL_EXPIRED,
    REFUSAL_FILE_ARGUMENT_CROSS_PROJECT,
    REFUSAL_FOREIGN_REFERENCE,
    REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE,
    REFUSAL_INSUFFICIENT_SCOPE,
    REFUSAL_NO_ACTING_USER,
    REFUSAL_NO_CREDENTIAL,
    REFUSAL_PLATFORM_DENIED,
    REFUSAL_PROJECT_IS_A_FLAG,
    REFUSAL_PROJECT_NOT_A_PATH,
    REFUSAL_PROJECT_NOT_OWNER_REPO,
    REFUSAL_QUICK_ACTION_CROSS_PROJECT,
    REFUSAL_REVOKED,
    REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT,
    REFUSAL_UNRECORDED_CROSS_PROJECT,
    REFUSAL_WRONG_HOST,
    TargetDecision,
    _names_a_bot_label,
    _record_cross_project_access,
    _validate_project,
    decide_target,
)
from automation.agent.middlewares.git_platform.gitlab import run_gitlab_subcommand
from codebase.base import GitPlatform
from codebase.models import CrossProjectAccessRecord
from core.constants import CROSS_PROJECT_CONTENT_MARKER, CrossProjectOutcome
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import (
    ATTACHED,
    CROSS_PROJECT,
    OTHER,
    VALID_POSITION,
    cross_project_runtime,
    installation_runtime,
    marked,
    patched_cross_project,
    run_gh,
    run_gl,
    service_settings,
)
from tests.unit_tests.conftest import recording_backend


class TestProjectValidation:
    """Rejected before any credential is read or any subprocess is spawned."""

    def test_empty_takes_the_attached_path(self):
        assert _validate_project("", ATTACHED, GitPlatform.GITLAB) == (None, None)
        assert _validate_project("   ", ATTACHED, GitPlatform.GITLAB) == (None, None)

    def test_equal_to_attached_takes_the_attached_path(self):
        assert _validate_project(ATTACHED, ATTACHED, GitPlatform.GITLAB) == (None, None)

    def test_leading_dash_is_flag_confusion(self):
        assert _validate_project("--project-id=99", ATTACHED, GitPlatform.GITLAB) == (None, REFUSAL_PROJECT_IS_A_FLAG)

    @pytest.mark.parametrize("value", ["group/ repo", "other/re\npo", "group\trepo", "group/repo\x00"])
    def test_whitespace_and_control_characters_are_rejected(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB)[1] == REFUSAL_PROJECT_NOT_A_PATH

    def test_another_project_is_accepted(self):
        assert _validate_project(OTHER, ATTACHED, GitPlatform.GITLAB) == (OTHER, None)

    def test_a_url_on_another_host_is_refused(self):
        _target, refusal = _validate_project("https://elsewhere.example/g/r", ATTACHED, GitPlatform.GITLAB)
        assert refusal == REFUSAL_WRONG_HOST.format(project="https://elsewhere.example/g/r", host="gitlab.com")

    def test_a_url_on_the_configured_host_reduces_to_its_path(self):
        assert _validate_project(f"https://gitlab.com/{OTHER}", ATTACHED, GitPlatform.GITLAB) == (OTHER, None)

    async def test_a_rejected_project_never_spawns_a_subprocess(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl("project-issue list", runtime, project="--oops", cross_project_enabled=True)
        assert result == REFUSAL_PROJECT_IS_A_FLAG
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "value",
        [
            "other.host/owner/repo",
            "owner/repo/extra",
            "evil.example:443/repo",
            "git@github.com:owner/repo",
            "owner",
            "owner/..",
            "https://github.com/owner/repo/issues/5",
        ],
    )
    def test_a_github_target_is_owner_and_name_only(self, value):
        """gh reads ``-R HOST/OWNER/REPO``: a third segment would send the person's token to that host."""
        assert _validate_project(value, ATTACHED, GitPlatform.GITHUB) == (None, REFUSAL_PROJECT_NOT_OWNER_REPO)

    @pytest.mark.parametrize(
        ("value", "target"),
        [("owner/repo", "owner/repo"), ("owner/repo.js", "owner/repo.js"), ("https://github.com/o/r", "o/r")],
    )
    def test_a_github_owner_and_name_is_accepted(self, value, target):
        assert _validate_project(value, ATTACHED, GitPlatform.GITHUB) == (target, None)

    @pytest.mark.parametrize("value", ["host:8443/group/repo", "group/../repo", "group//repo", "group/re@po"])
    def test_a_gitlab_path_carries_no_host_or_traversal(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB) == (None, REFUSAL_PROJECT_NOT_A_PATH)

    @pytest.mark.parametrize("value", ["group/sub/repo", "my.group/repo", "12345"])
    def test_a_gitlab_path_may_nest_groups(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB) == (value, None)

    async def test_a_github_host_in_the_project_never_reaches_gh(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh("issue list", runtime, project="other.host/owner/repo", cross_project_enabled=True)
        assert result == REFUSAL_PROJECT_NOT_OWNER_REPO
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()


class TestExistingLimitsApplyCrossProject:
    """The allowlist, the blocked GitHub `api` resource and the large-result eviction behave
    identically against another project."""

    async def test_disallowed_gitlab_subcommand_is_refused_identically(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-variable delete --key SECRET", runtime, project=OTHER, cross_project_enabled=True
            )
        assert result == "error: The subcommand 'project-variable' is not allowed by policy."
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_github_api_resource_stays_blocked_cross_project(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh("api /repos/other/other/issues", runtime, project=OTHER, cross_project_enabled=True)
        assert result == "error: The subcommand 'api' is not allowed by policy."
        mocks.create_proc.assert_not_called()

    async def test_oversized_cross_project_result_is_written_to_the_same_dir(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        backend = recording_backend()
        with patched_cross_project(stdout=b'[{"iid": 1}]\n'):
            result = await run_gitlab_subcommand(
                "project-merge-request list",
                runtime,
                "simplified",
                True,
                backend=backend,
                project=OTHER,
                cross_project_enabled=True,
            )
        path, _content = backend.awrite.call_args.args
        assert path == "/workspace/large_tool_results/c-xproj"
        assert result.startswith("Wrote ")


class TestRefusalVocabulary:
    """One string per cause, each naming the project and the next step."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (CredentialReason.NO_ACTING_USER, REFUSAL_NO_ACTING_USER.format(attached=ATTACHED)),
            (CredentialReason.NO_CREDENTIAL, REFUSAL_NO_CREDENTIAL.format(person="Ada", provider="gitlab")),
            (CredentialReason.EXPIRED, REFUSAL_EXPIRED.format(person="Ada", provider="gitlab")),
            (CredentialReason.REVOKED, REFUSAL_REVOKED.format(person="Ada", provider="gitlab")),
            (
                CredentialReason.INSUFFICIENT_SCOPE,
                REFUSAL_INSUFFICIENT_SCOPE.format(person="Ada", provider="gitlab", project=OTHER),
            ),
            (CredentialReason.DISABLED, REFUSAL_DISABLED.format(attached=ATTACHED)),
        ],
    )
    async def test_each_reason_gets_its_own_string(self, reason, expected):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(resolved=ResolvedCredential(reason=reason)) as mocks:
            result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert result.startswith("error: ")
        assert result == expected
        mocks.create_proc.assert_not_called()

    async def test_platform_denial_is_ambiguous_between_absent_and_forbidden(self):
        """The tool must not become an existence oracle for private projects."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(returncode=1, stderr=b"404: 404 Project Not Found"):
            result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert result == REFUSAL_PLATFORM_DENIED.format(project=OTHER, person="Ada", provider="gitlab")
        assert "may not have access" in result
        assert "may not exist" in result

    async def test_stderr_is_never_echoed_on_the_cross_project_path(self):
        """Stderr can carry token fragments and names the person may not see."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        secret = "glpat-SECRETVALUE and secret-group/secret-repo"  # noqa: S105
        with patched_cross_project(returncode=1, stderr=secret.encode()):
            result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert "glpat-SECRETVALUE" not in result
        assert "secret-group/secret-repo" not in result
        assert result.startswith("error: ")

    async def test_attached_project_failures_still_report_stderr(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(returncode=1, stderr=b"boom"):
            result = await run_gl("project-issue list", runtime, project="", cross_project_enabled=True)
        assert "boom" in result


def _refusing_resolver(reason=CredentialReason.NO_CREDENTIAL):
    return AsyncMock(return_value=ResolvedCredential(reason=reason))


async def _call_cross_project(runtime, resolve, record=None):
    with (
        patch(f"{CROSS_PROJECT}.aresolve_access_token", resolve),
        patch(f"{CROSS_PROJECT}._record_cross_project_access", record or AsyncMock()),
        patch(f"{CROSS_PROJECT}._acting_person_label", AsyncMock(return_value="Alice")),
    ):
        return await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)


async def test_signed_in_run_resolves_by_user_id():
    resolve = _refusing_resolver()
    await _call_cross_project(cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7), resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "acting_user_id": 7}


async def test_webhook_run_resolves_by_platform_uid_only():
    resolve = _refusing_resolver()
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "platform_uid": "4242"}


async def test_a_platform_uid_wins_over_a_user_id():
    record = AsyncMock()
    resolve = _refusing_resolver()
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve, record)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "platform_uid": "4242"}
    # The uid's grant was looked up, so the sign-in it was not spent from must not be named.
    assert record.await_args.kwargs["acting_user_id"] is None


async def test_a_policy_refusal_beside_a_platform_uid_names_no_sign_in():
    record = AsyncMock()
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_platform_uid="4242")
    with patch(f"{CROSS_PROJECT}._record_cross_project_access", record):
        await run_gl("project-branch create --branch x --ref main", runtime, project=OTHER, cross_project_enabled=True)
    assert record.await_args.kwargs["acting_user_id"] is None


async def test_an_unauthenticated_user_id_is_never_spent():
    resolve = _refusing_resolver(CredentialReason.NO_ACTING_USER)
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_user_authenticated=False)
    result = await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB}
    assert result.startswith("error:")


async def test_an_authenticated_run_without_a_user_is_refused():
    resolve = AsyncMock(wraps=aresolve_access_token)
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=None)
    with patch("accounts.credentials._acapability_enabled", AsyncMock(return_value=True)):
        result = await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB}
    assert result == REFUSAL_NO_ACTING_USER.format(attached=ATTACHED)


async def test_webhook_run_refused_when_webhook_runs_disabled():
    record = AsyncMock()
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    result = await _call_cross_project(runtime, _refusing_resolver(CredentialReason.WEBHOOK_RUNS_DISABLED), record)
    assert "issue or merge request event" in result
    assert record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_DISABLED


async def test_the_audit_row_names_the_resolved_owner_on_a_webhook_run():
    record = AsyncMock()
    resolve = AsyncMock(return_value=ResolvedCredential(reason=CredentialReason.REVOKED, user_id=31))
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve, record)
    assert record.await_args.kwargs["acting_user_id"] == 31


async def test_a_signed_in_refusal_is_attributed_to_the_signed_in_user():
    record = AsyncMock()
    await _call_cross_project(cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7), _refusing_resolver(), record)
    assert record.await_args.kwargs["acting_user_id"] == 7


async def test_an_unauthenticated_user_id_is_never_recorded():
    record = AsyncMock()
    runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_user_authenticated=False)
    with patch(f"{CROSS_PROJECT}._record_cross_project_access", record):
        await run_gl("project-branch create --branch x --ref main", runtime, project=OTHER, cross_project_enabled=True)
    assert record.await_args.kwargs["acting_user_id"] is None


class TestPlatformFailureClassificationIsAnchored:
    """``401`` was matched as a bare substring against CLI stderr, ahead of the 404/403 list, and
    a match destroyed the person's grant. The CLIs echo the requested object's number, so an
    ordinary not-found on issue 401 read as a dead token."""

    @pytest.mark.parametrize(
        "stderr",
        [
            b"GraphQL: Could not resolve to an Issue with the number of 401. (repository.issue)",
            b"HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/401)",
            b"404 Not Found: run 1401 does not exist",
            b"could not find branch ticket-401",
        ],
        ids=["issue-401", "404-url-with-401", "run-1401", "branch-name"],
    )
    async def test_an_ordinary_not_found_does_not_touch_the_grant(self, stderr):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(returncode=1, stderr=stderr) as mocks:
            result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

        mocks.invalidate.assert_not_awaited()
        assert result != REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")

    @pytest.mark.parametrize(
        "stderr",
        [b"401 Unauthorized", b"HTTP 401: Bad credentials", b"error: invalid_token", b"401: Unauthorized"],
        ids=["gitlab", "gh", "invalid-token", "gitlab-colon"],
    )
    async def test_a_real_auth_failure_drops_the_cached_token_without_revoking(self, stderr):
        """The stored grant is authoritative only via the refresh endpoint. Dropping the cached
        token makes the next call re-resolve, which refreshes or refuses on the platform's word."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(returncode=1, stderr=stderr) as mocks:
            result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

        mocks.invalidate.assert_awaited_once()
        assert result == REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")

    async def test_a_failed_cache_drop_still_refuses_and_logs_no_traceback(self, caplog):
        """A traceback carries this frame's locals to Sentry, and stderr can echo the person's token."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project(returncode=1, stderr=b"401 Unauthorized glpat-PERSONTOKEN") as mocks:
            mocks.invalidate.side_effect = ConnectionError("cache down")
            with caplog.at_level("DEBUG"):
                result = await run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")
        assert all(record.exc_info is None for record in caplog.records)
        assert "glpat-PERSONTOKEN" not in caplog.text


class TestDestructiveSubcommandsAreRefusedCrossProject:
    """The person genuinely holds the permission, so the platform will not stop these. The action,
    though, can be chosen by issue text somebody else wrote — so refuse the destructive verbs by
    policy outside the attached project, and leave them available on it."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project delete-merged-branches",
            "project trigger-pipeline --ref main",
            "project-pipeline cancel --pipeline-id 3",
            "project-pipeline retry --pipeline-id 3",
            "project-pipeline create --ref main",
            "project-branch create --branch x --ref main",
            "project-tag create --tag-name v1 --ref main",
            "project-release create --tag-name v1",
            "project-job retry --job-id 4",
            "project-merge-request-draft-note delete --mr-iid 1 --draft-note-id 2",
        ],
    )
    async def test_gitlab_destructive_verbs_are_refused(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        mocks.create_proc.assert_not_called()
        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue close 1",
            "pr close 2",
            "pr reopen 2",
            "run rerun 5",
            "workflow run ci.yml",
            "cache delete 9",
            "label create bug",
            "label edit bug --color ffffff",
        ],
    )
    async def test_github_destructive_verbs_are_refused(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        mocks.create_proc.assert_not_called()
        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )

    @pytest.mark.parametrize(
        "subcommand", ["project-pipeline cancel --pipeline-id 3", "project delete-merged-branches"]
    )
    async def test_the_attached_project_keeps_them(self, subcommand):
        """Nothing about the attached project changes: it still runs under DAIV's own identity."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl(subcommand, runtime, project="", cross_project_enabled=True)

        mocks.create_proc.assert_called_once()

    async def test_reads_and_comments_still_cross(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl(
                'project-issue-note create --issue-iid 1 --body "hi"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        mocks.create_proc.assert_called_once()


class TestGitLabFlagSpellings:
    """python-gitlab's subcommand parsers accept any unambiguous prefix of an option, so the policy reads
    each flag as the CLI will: ``--state-ev`` is ``--state-event`` and ``--desc`` is ``--description``."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("project-merge-request update --iid 1 --state-ev close", "--state-event"),
            ("project-issue update --iid 1 --state close", "--state-event"),
            ("project-issue update --iid 1 --state-ev=close", "--state-event"),
            ("project-merge-request update --iid 1 --target-br main", "--target-branch"),
            ("project-merge-request update --iid 1 --assignee-id 3", "--assignee-id"),
            ("project-issue update --iid 1 --discussion-l true", "--discussion-locked"),
        ],
    )
    async def test_an_abbreviated_denied_flag_is_refused(self, subcommand, flag):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action} {flag}", attached=ATTACHED, project=OTHER
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand", ["project-issue list --state opened", "project-merge-request list --target-branch main"]
    )
    async def test_a_read_filter_sharing_a_denied_name_still_crosses(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert mocks.create_proc.call_args.args[1 : 1 + len(subcommand.split())] == tuple(subcommand.split())

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue create --title t --desc "hello"',
            'project-issue create --title t --desc="hello"',
            'project-issue-note create --issue-iid 1 --bo "hello"',
        ],
    )
    async def test_an_abbreviated_body_flag_is_marked(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert marked(mocks.create_proc.call_args.args, "hello")

    async def test_every_repeated_body_flag_is_marked(self):
        """argparse keeps the last value, so marking only the first would publish the second unmarked."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl(
                'project-issue create --title t --description "one" --description "two"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        argv = mocks.create_proc.call_args.args
        assert marked(argv, "one")
        assert marked(argv, "two")

    async def test_an_unknown_or_ambiguous_option_is_refused_before_any_credential(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-issue update --iid 1 --assignee 3", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(flag="--assignee", command="project-issue update")
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue create --title @/proc/self/environ --description x",
            "project-issue create --title=@/proc/self/environ --description x",
            "project-issue list --search @/etc/hostname",
        ],
    )
    async def test_a_value_read_from_a_file_is_refused(self, subcommand):
        """python-gitlab reads ``@path`` from the worker's disk, where ``/proc/self/environ`` holds the token."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_FILE_ARGUMENT_CROSS_PROJECT
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    async def test_an_escaped_at_sign_still_crosses(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl(
                'project-issue-note create --issue-iid 1 --body "@@alice please look"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert marked(mocks.create_proc.call_args.args, "@@alice please look")


class TestGitHubFlagSpellings:
    """gh's flag parser reads ``-m X``, ``-mX``, ``-m=X``, ``--milestone=X`` and clusters such as ``-db X`` alike."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("issue edit 1 -m v1", "--milestone"),
            ("issue edit 1 -mv1", "--milestone"),
            ("issue edit 1 -m=v1", "--milestone"),
            ("issue edit 1 --milestone=v1", "--milestone"),
            ("pr edit 2 -B other", "--base"),
            ("pr edit 2 --base=other", "--base"),
            ("issue create -t T -b x -wm v1", "--milestone"),
            ("issue comment 1 --edit-last", "--edit-last"),
            ("pr comment 2 --edit-last -b x", "--edit-last"),
        ],
    )
    async def test_a_short_or_attached_denied_flag_is_refused(self, subcommand, flag):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action} {flag}", attached=ATTACHED, project=OTHER
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 -Fnote.md",
            "issue comment 1 -F=note.md",
            "issue comment 1 -F note.md",
            "issue create -t T -T template.md",
            "issue create -t T -wTtemplate.md",
            "issue create -t T --recover state.json",
        ],
    )
    async def test_a_body_the_marker_cannot_reach_is_refused(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_BODY_FILE_CROSS_PROJECT
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 -bhello",
            "issue comment 1 -b=hello",
            "issue comment 1 --body=hello",
            "issue comment 1 -b hello",
            "issue create -t T -wb hello",
            "issue edit 1 -b first --body hello",
        ],
    )
    async def test_every_spelling_of_the_body_is_marked(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert marked(mocks.create_proc.call_args.args, "hello")

    @pytest.mark.parametrize("subcommand", ["issue comment 1 -xbhello", "issue create -t T -wRelsewhere/repo"])
    async def test_a_cluster_with_an_unknown_letter_is_refused(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag=subcommand.split()[-1], command=" ".join(subcommand.split()[:2])
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_a_read_sharing_a_short_body_letter_is_left_alone(self):
        """``run list -b`` is ``--branch``: marking it would break the read."""
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            await run_gh("run list -b main", runtime, project=OTHER, cross_project_enabled=True)

        assert mocks.create_proc.call_args.args[:5] == ("gh", "run", "list", "-b", "main")


class TestBotLabelsAreNotAddedCrossProject:
    """A body marker cannot cover a label, and a label event starts a DAIV run in the other project as the person."""

    REFUSED_GITLAB = [
        "project-issue update --iid 1 --labels daiv",
        "project-issue update --iid 1 --labels=daiv",
        "project-issue update --iid 1 --label daiv",
        "project-issue update --iid 1 --lab=daiv",
        "project-issue update --iid 1 --labels bug,daiv-max",
        'project-issue update --iid 1 --labels "bug, DAIV-Auto"',
        "project-issue update --iid 1 --labels bug --labels daiv",
        "project-issue create --title t --description x --labels daiv",
        "project-merge-request update --iid 1 --labels daiv-auto",
    ]
    REFUSED_GITHUB = [
        "issue create -t T -b x --label daiv",
        "issue create -t T -b x --label=daiv",
        "issue create -t T -b x -l daiv",
        "issue create -t T -b x -ldaiv",
        "issue create -t T -b x -l=daiv",
        "issue create -t T -b x --label bug,daiv-max",
        "issue create -t T -b x --label bug --label daiv",
        "issue create -t T -b x -l DAIV-Auto",
        "issue create -t T -b x -wl daiv",
        "issue edit 1 --add-label daiv",
        "issue edit 1 --add-label=DAIV",
        "pr edit 2 --add-label bug,daiv-auto",
        "pr edit 2 --add-label bug --add-label daiv",
        "issue edit 1 --add-label '\"daiv\"'",
    ]

    PADDINGS = ["\xa0", "\u3000", "\u2003", "\u2028", "\x0b", "\x0c", "\x1f", "\x85"]
    PADDED_BOT_LABELS = [
        *(f"daiv{pad}" for pad in PADDINGS),
        *(f"{pad}daiv" for pad in PADDINGS),
        *(f"{pad}DAIV-Max{pad}" for pad in PADDINGS),
        *(f'"{pad}daiv-auto{pad}"' for pad in PADDINGS),
        "daiv\u200b",
        "\u200bdaiv",
        "\ufeffdaiv",
        "\uff44\uff41\uff49\uff56",
    ]

    @staticmethod
    def _assert_refused_without_spending(result, mocks):
        assert result == REFUSAL_BOT_LABEL_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize("subcommand", REFUSED_GITLAB)
    async def test_gitlab_refuses_a_bot_label_before_any_credential(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("subcommand", REFUSED_GITHUB)
    async def test_github_refuses_a_bot_label_before_any_credential(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", PADDED_BOT_LABELS, ids=ascii)
    async def test_gitlab_refuses_a_padded_bot_label(self, label):
        """python-gitlab ``strip()``s every label, so ``daiv`` plus any Unicode whitespace is still ``daiv``."""
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                f"project-issue update --iid 1 --labels 'bug,{label}'",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", PADDED_BOT_LABELS, ids=ascii)
    @pytest.mark.parametrize("subcommand", ["issue edit 1 --add-label {}", "issue create -t T -b x --label {}"])
    async def test_github_refuses_a_padded_bot_label(self, subcommand, label):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand.format(f"'{label}'"), runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", ["bug\xa0", "\u3000daiv-docs", "good first issue", "daiv\xa0docs"], ids=ascii)
    async def test_a_padded_label_that_is_not_a_bot_label_still_crosses(self, label):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                f"project-issue update --iid 1 --labels 'bug,{label}'",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        assert f"bug,{label}" in mocks.create_proc.call_args.args

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("daiv", True),
            ("bug, DAIV-Auto ,x", True),
            ("\x00daiv\x00", True),
            ("'daiv'", True),
            ("bug\uff0cdaiv", True),
            ("daiv-docs", False),
            ("good first issue", False),
            ("", False),
        ],
        ids=ascii,
    )
    def test_names_a_bot_label(self, value, expected):
        assert _names_a_bot_label(value) is expected

    async def test_gitlab_has_no_add_labels_flag_to_slip_past(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-issue update --iid 1 --add-labels daiv", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag="--add-labels", command="project-issue update"
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue update --iid 1 --labels bug,daiv-docs",
            "project-issue update --iid 1 --labels bug",
            "project-issue create --title t --description x --labels triage",
            "project-issue list --labels daiv",
            "project-merge-request list --labels daiv-max",
        ],
    )
    async def test_gitlab_other_labels_and_label_filters_still_cross(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert subcommand.split()[-1] in mocks.create_proc.call_args.args

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue create -t T -b x --label bug,daiv-docs",
            "issue create -t T -b x -l triage",
            "issue create -t T -b x -wl bug",
            "issue edit 1 --add-label bug",
            "issue edit 1 --remove-label daiv",
            "pr edit 2 --remove-label daiv-max",
            "issue list --label daiv",
            "pr list --label daiv-max",
        ],
    )
    async def test_github_other_labels_removals_and_label_filters_still_cross(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert mocks.create_proc.call_args.args[:3] == ("gh", *subcommand.split()[:2])

    async def test_the_attached_project_keeps_the_label_on_gitlab(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-issue update --iid 1 --labels daiv", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"
        assert mocks.create_proc.call_args.args[:6] == ("gitlab", "project-issue", "update", "--iid", "1", "--labels")
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.record.assert_not_called()

    async def test_the_attached_project_keeps_the_label_on_github(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            decision = await decide_target(
                "issue",
                "edit",
                ["1", "--add-label", "daiv"],
                "",
                runtime,
                provider=GitPlatform.GITHUB,
                cross_project_enabled=True,
            )

        assert decision == TargetDecision()
        mocks.resolve.assert_not_called()
        mocks.record.assert_not_called()


class TestGitLabQuickActionsAreRefusedCrossProject:
    """GitLab runs a body line starting with ``/`` as the person, past every denied verb and flag, so the line is
    refused rather than published."""

    @staticmethod
    def _note(body):
        return f'project-issue-note create --issue-iid 1 --body "{body}"'

    @staticmethod
    def _description(body):
        return f'project-issue create --title t --description "{body}"'

    @pytest.mark.parametrize("body", ["/label ~daiv", "/close", "  /merge", "\t/assign @bob", "hello\n/close"])
    @pytest.mark.parametrize("build", [_note, _description], ids=["note", "description"])
    async def test_a_quick_action_line_is_refused_before_any_credential(self, build, body):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(build(body), runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_QUICK_ACTION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue create --title t --desc "/close"',
            "project-issue create --title t --desc=/close",
            "project-issue create --title t --description=/close",
            'project-issue-note create --issue-iid 1 --bo "/close"',
            'project-merge-request-note create --mr-iid 1 --body "/merge"',
            'project-issue-discussion create --issue-iid 1 --body "/label ~daiv"',
            'project-issue update --iid 1 --description "/reopen"',
            'project-merge-request-draft-note create --mr-iid 1 --note "/merge"',
        ],
    )
    async def test_every_spelling_of_a_body_flag_is_checked(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_QUICK_ACTION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize("body", ["see a/b", "docs live in /etc/hosts, not here", "https://example.com/a/b"])
    @pytest.mark.parametrize("build", [_note, _description], ids=["note", "description"])
    async def test_a_slash_inside_a_line_still_crosses_marked(self, build, body):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(build(body), runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert marked(mocks.create_proc.call_args.args, body)

    async def test_a_slash_in_a_flag_that_is_not_a_body_still_crosses(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                'project-issue list --search "/close"', runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == "ok"
        mocks.create_proc.assert_called_once()

    async def test_the_attached_project_keeps_quick_actions(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(self._note("/close"), runtime, project="", cross_project_enabled=True)

        assert result == "ok"
        assert "/close" in mocks.create_proc.call_args.args
        assert not any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in mocks.create_proc.call_args.args)
        mocks.record.assert_not_called()

    async def test_github_has_no_quick_actions_to_refuse(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh('issue comment 1 --body "/close"', runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert marked(mocks.create_proc.call_args.args, "/close")


DENIED = "secret-group/secret-repo"
TARGET_CONTENT = "ACQUISITION-CODENAME-BLUEBIRD"


async def _run_gl_audited(
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
    """``(result, create_proc, resolve)`` with the real audit writer. ``resolved=None`` leaves the
    resolve mock unconfigured, for the tests asserting no credential was spent."""
    proc = Mock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    with (
        patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.gitlab.settings", service_settings()),
        patch(f"{CROSS_PROJECT}.aresolve_access_token") as resolve,
        patch(f"{CROSS_PROJECT}.ainvalidate_cached_token", AsyncMock(return_value=None)),
    ):
        create_proc.return_value = proc
        if resolved is not None:
            resolve.return_value = resolved
        if side_effect is not None:
            create_proc.side_effect = side_effect
        result = await run_gitlab_subcommand(
            subcommand,
            runtime,
            "simplified",
            False,
            backend=recording_backend(),
            project=project,
            cross_project_enabled=cross_project_enabled,
        )
    return result, create_proc, resolve


@pytest.mark.django_db(transaction=True)
class TestNoFallbackToTheServiceIdentity:
    """The natural "fall back so the agent gets an answer" instinct is precisely the leak this forbids."""

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
    async def test_a_credential_denial_spawns_no_process_at_all(self, member_user, reason):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime, project=DENIED, resolved=ResolvedCredential(reason=reason)
        )

        assert result.startswith("error: ")
        create_proc.assert_not_called()

        records = [r async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert len(records) == 1
        assert records[0].outcome != CrossProjectAccessRecord.Outcome.ALLOWED
        # The row names the person the run acts for, never DAIV itself, and survives their deletion.
        assert records[0].acting_user_id == member_user.pk
        assert records[0].acting_user_label

    async def test_a_platform_denial_is_not_retried_under_the_service_token(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stderr=b"404 Project Not Found",
        )

        assert result.startswith("error: ")
        assert create_proc.call_count == 1
        assert create_proc.call_args.kwargs["env"]["GITLAB_OAUTH_TOKEN"] == "person-token"  # noqa: S105
        assert "GITLAB_PRIVATE_TOKEN" not in create_proc.call_args.kwargs["env"]

        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_NO_ACCESS]
        assert (
            await CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED, acting_user_id=member_user.pk).acount()
            == 1
        )


@pytest.mark.django_db(transaction=True)
class TestNoTargetContentSurvivesADenial:
    """Nothing from the refused project may appear in the answer, the audit row, or a state update."""

    async def test_denied_content_is_absent_from_the_result_and_the_record(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stdout=TARGET_CONTENT.encode(),
            stderr=f"403 Forbidden while reading {TARGET_CONTENT}".encode(),
        )

        assert TARGET_CONTENT not in result
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id=DENIED)
        assert TARGET_CONTENT not in str(record.__dict__)

    async def test_a_denial_returns_a_plain_string_not_a_state_update(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITHUB, acting_user_id=member_user.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(TARGET_CONTENT.encode(), b"404 Not Found"))
        proc.returncode = 1
        with (
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
            patch(
                f"{CROSS_PROJECT}.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch(f"{CROSS_PROJECT}.ainvalidate_cached_token", AsyncMock(return_value=None)),
        ):
            create_proc.return_value = proc
            result = await run_gh("issue list", runtime, project=DENIED, cross_project_enabled=True)

        assert not isinstance(result, Command)
        assert TARGET_CONTENT not in result
        assert runtime.state == {}


@pytest.mark.django_db(transaction=True)
class TestThePersonsTokenNeverEntersState:
    """Agent state is checkpointed, so a person's token in it is a token at rest under a key nobody revokes."""

    async def test_a_successful_cross_project_github_call_produces_no_command(self, member_user, xproj_session):
        runtime = cross_project_runtime(GitPlatform.GITHUB, acting_user_id=member_user.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        proc.returncode = 0
        with (
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
            patch(
                f"{CROSS_PROJECT}.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch("automation.agent.middlewares.git_platform.github._get_cached_github_cli_token") as cached_mock,
        ):
            create_proc.return_value = proc
            result = await run_gh("issue list", runtime, project="other/repo", cross_project_enabled=True)

        assert result == "ok"
        assert not isinstance(result, Command)
        assert runtime.state == {}
        cached_mock.assert_not_called()

    async def test_an_allowed_cross_project_call_is_recorded_without_the_token(self, member_user, xproj_session):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project="other/repo",
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
        )

        assert result == "ok"
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id="other/repo")
        assert record.outcome == CrossProjectAccessRecord.Outcome.ALLOWED
        assert record.acting_user_id == member_user.pk
        assert record.thread_id == "t-xproj"
        assert "person-token" not in str(record.__dict__)


@pytest.mark.django_db(transaction=True)
class TestNoTokenReachesALogRecord:
    """Exception messages are where secrets usually escape."""

    async def test_a_failed_cross_project_call_logs_no_token(self, member_user, caplog):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with caplog.at_level("DEBUG"):
            result, _, _ = await _run_gl_audited(
                runtime,
                project=DENIED,
                resolved=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
                returncode=1,
                stderr=b"remote: HTTP Basic: Access denied for glpat-PERSONTOKEN",
            )

        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text

    async def test_a_subprocess_failure_logs_no_token(self, member_user, caplog):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with (
            caplog.at_level("DEBUG"),
            patch(
                "automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec",
                side_effect=OSError("spawn failed"),
            ),
            patch("automation.agent.middlewares.git_platform.gitlab.settings", service_settings()),
            patch(
                f"{CROSS_PROJECT}.aresolve_access_token",
                return_value=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
            ),
        ):
            result = await run_gl("project-issue list", runtime, project=DENIED, cross_project_enabled=True)

        assert result.startswith("error: ")
        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text


@pytest.mark.django_db(transaction=True)
class TestEveryAttemptLeavesARow:
    """The attempts that spend the token and then fail are the ones most worth a row."""

    async def test_a_timeout_is_recorded(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=TimeoutError,
        )

        assert "timed out" in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_failure_to_launch_is_recorded(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=OSError("spawn failed"),
        )

        assert result.startswith("error: ")
        assert "spawn failed" not in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_disallowed_subcommand_is_recorded(self, member_user):
        """The allow-list runs before the target decision, so its refusals need their own row."""
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand="project-hook create --url http://evil.test",
        )

        assert "not allowed by policy" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_capability_being_off_is_recorded_as_such(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(runtime, project=DENIED, cross_project_enabled=False)

        assert "not enabled" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_DISABLED]


@pytest.mark.django_db(transaction=True)
class TestPolicyRefusalsSpendNothing:
    """Resolving a credential before a denied verb would rotate a GitLab refresh token for a call that can never run."""

    @pytest.mark.parametrize(
        ("subcommand", "why"),
        [
            ("project-branch create --branch x --ref main", "denied verb"),
            ("project-issue move --iid 1 --to-project-id 9", "relocates an issue out of the project"),
            ("project-issue update --iid 1 --state-event close", "closes through an allowed verb"),
            ("project-merge-request update --iid 1 --target-branch other", "repoints an MR"),
            ("project-merge-request-note update --mr-iid 1 --note-id 2 --body x", "edits someone else's note"),
            ("project-label create --name x --color '#fff'", "changes project configuration"),
            ("project-issue update --iid 1 --labels daiv", "adds the label that starts a DAIV run there"),
            ('project-issue-note create --issue-iid 1 --body "/close"', "a quick action runs as the person"),
            ("project-merge-request update --iid 1 --state-ev close", "closes through an abbreviated flag"),
            (
                f"project-merge-request-discussion create --mr-iid 1 --position '{json.dumps(VALID_POSITION)}'",
                "an inline diff comment goes through the service token",
            ),
        ],
    )
    async def test_a_policy_refusal_resolves_no_credential(self, member_user, subcommand, why):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, resolve = await _run_gl_audited(runtime, project=DENIED, subcommand=subcommand)

        assert result.startswith("error: "), why
        create_proc.assert_not_called()
        resolve.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_same_verbs_still_work_on_the_attached_project(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand="project-issue update --iid 1 --state-event close",
        )

        assert not result.startswith("error: ")
        assert create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert not await CrossProjectAccessRecord.objects.aexists()


@pytest.mark.django_db(transaction=True)
class TestNoUnattributedCrossProjectWrite:
    """A cross-project write carries a person's attribution, so only the marker tells the webhook it is DAIV's."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue-note create --issue-iid 1 --body "hello"',
            'project-issue-note create --body="hello" --issue-iid 1',
            'project-issue create --title t --description "hello"',
            'project-issue create --title t --description="hello"',
        ],
    )
    async def test_a_published_body_carries_the_marker(self, member_user, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        _, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand=subcommand,
        )

        argv = create_proc.call_args.args
        assert any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_the_attached_project_body_is_left_alone(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        _, create_proc, _ = await _run_gl_audited(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand='project-issue-note create --issue-iid 1 --body "hello"',
        )

        argv = create_proc.call_args.args
        assert not any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_a_body_read_from_a_file_is_refused_rather_than_published_unmarked(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, resolve = await _run_gl_audited(
            runtime,
            project=DENIED,
            subcommand="project-issue create --title t --body-file /tmp/note.md",  # noqa: S108
        )

        assert result.startswith("error: ")
        create_proc.assert_not_called()
        resolve.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestWebhookRunAttribution:
    """A webhook's platform uid decides whose grant is spent, so an unmatched uid is attributed to nobody, even
    when somebody else's grant exists."""

    async def test_an_unmatched_platform_uid_is_attributed_to_nobody(self, member_user):
        from asgiref.sync import sync_to_async

        from accounts.credentials import platform_host
        from accounts.models import PlatformCredential

        def _grant_alice_uid_77():
            credential = PlatformCredential(
                user=member_user,
                provider=GitPlatform.GITLAB.value,
                host=platform_host(GitPlatform.GITLAB),
                platform_uid="77",
                scopes=["api"],
            )
            credential.access_token = "alice-token"  # noqa: S105
            credential.save()

        await sync_to_async(_grant_alice_uid_77)()
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="999")
        with (
            patch("accounts.credentials.site_settings") as credential_settings,
            patch("automation.agent.middlewares.git_platform.cli.asyncio.create_subprocess_exec") as create_proc,
        ):
            credential_settings.cross_project_access_enabled = True
            credential_settings.cross_project_webhook_runs_enabled = True
            result = await run_gl("project-issue list", runtime, project=DENIED, cross_project_enabled=True)

        create_proc.assert_not_called()
        assert result == REFUSAL_NO_CREDENTIAL.format(person="The requesting user", provider="gitlab")
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id=DENIED)
        assert record.acting_user_id is None
        assert record.acting_user_label == "The requesting user"
        assert record.outcome == CrossProjectAccessRecord.Outcome.DENIED_NO_CREDENTIAL


class TestGitLabConfidentialityStaysPut:
    """Turning ``--confidential`` off on someone else's issue is a disclosure, not an edit."""

    @pytest.mark.parametrize(
        "flag", ["--confidential false", "--confidential=false", "--confid false", "--c=false", "--confidential true"]
    )
    async def test_an_update_naming_it_in_any_spelling_is_refused(self, flag):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                f"project-issue update --iid 1 {flag}", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action="project-issue update --confidential", attached=ATTACHED, project=OTHER
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    async def test_creating_a_confidential_issue_still_crosses(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-issue create --title t --description d --confidential true",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        assert "--confidential" in mocks.create_proc.call_args.args

    async def test_the_attached_project_keeps_it(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(
                "project-issue update --iid 1 --confidential false", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"
        mocks.resolve.assert_not_called()


class TestGitHubWritesTakeOnlyKnownFlags:
    """gh is installed unpinned, so a flag the gh 2.45 table does not list may write in ways nobody checked."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("issue comment 1 --delete-last --yes", "--delete-last"),
            ("pr comment 2 --delete-last", "--delete-last"),
            ("issue edit 1 --remove-milestone", "--remove-milestone"),
            ("issue edit 1 --set-something x", "--set-something"),
            ("issue edit 1 --set-something=x", "--set-something"),
            ("issue create -t T -b x --type bug", "--type"),
            ("pr edit 2 --title T --auto-merge", "--auto-merge"),
        ],
    )
    async def test_an_unknown_long_flag_is_refused_before_any_credential(self, subcommand, flag):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag=flag, command=" ".join(subcommand.split()[:2])
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 --body hello",
            "issue comment 1 --body=hello --web",
            "issue edit 1 --title T --add-label bug --add-project Roadmap --body hello",
            "issue create --title T --body hello --assignee me --project Roadmap",
            "pr edit 2 --add-reviewer bob --remove-label wip --body hello",
            "pr comment 2 -b hello --help",
        ],
    )
    async def test_known_flags_still_cross(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert marked(mocks.create_proc.call_args.args, "hello")

    async def test_the_attached_project_keeps_unknown_flags(self):
        with patched_cross_project() as mocks:
            result = await run_gh(
                "issue comment 1 --edit-last --body x", installation_runtime(), project="", cross_project_enabled=True
            )

        assert result == "ok"
        assert "--edit-last" in mocks.create_proc.call_args.args


class TestCrossProjectCreationAndTimeResets:
    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue reset-spent-time --iid 1",
            "project-issue reset-time-estimate --iid 1",
            "project-merge-request reset-spent-time --iid 1",
            "project-merge-request reset-time-estimate --iid 1",
            "project-merge-request create --source-branch f --target-branch main --title t",
        ],
    )
    async def test_gitlab_refuses_them(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize("subcommand", ["pr create -t T -b x", "pr create --fill", "pr create -df"])
    async def test_github_refuses_a_pull_request(self, subcommand):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(action="pr create", attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()

    async def test_the_attached_project_keeps_them(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project():
            result = await run_gl(
                "project-issue reset-spent-time --iid 1", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"


class TestAReferenceToAnotherRepository:
    """gh reads the repository from an issue or pull request URL rather than from ``--repo``."""

    FOREIGN = [
        "issue view https://github.com/acme/secret/issues/1",
        "issue view HTTPS://GITHUB.COM/acme/secret/issues/1",
        "pr review https://github.com/acme/secret/pull/1 --approve",
        "pr view https://github.com/acme/secret/pull/1/files",
        "pr diff --color never https://github.com/acme/secret/pull/1",
        "issue comment https://github.com/acme/secret/issues/1 --body x",
        "issue view -c https://github.com/acme/secret/issues/1",
        "issue view -- https://github.com/acme/secret/issues/1",
        "issue view acme/secret#1",
        "issue view https://ghe.example.com/group/repo/issues/1",
    ]

    @pytest.mark.parametrize("subcommand", FOREIGN)
    async def test_the_attached_path_refuses_it_and_points_at_project(self, subcommand):
        reference = next(arg for arg in subcommand.split() if "/" in arg)
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, installation_runtime(), project="", cross_project_enabled=True)

        assert result == REFUSAL_FOREIGN_REFERENCE.format(reference=reference, target=ATTACHED)
        assert "`project`" in result
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_with_the_capability_off_it_says_other_repositories_are_unavailable(self):
        subcommand = "issue view https://github.com/acme/secret/issues/1"
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, installation_runtime(), project="", cross_project_enabled=False)

        assert result == REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE.format(
            reference="https://github.com/acme/secret/issues/1", target=ATTACHED
        )
        assert "`project`" not in result
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue view https://github.com/Group/Repo/issues/1",
            "pr view https://github.com/group/repo/pull/2",
            "issue view group/repo#1",
            "issue view 1",
            "issue comment 1 --body https://github.com/acme/secret/issues/1",
            "issue view 1 --json url --jq .url",
            "issue view 1 -t https://github.com/acme/secret/issues/1",
            "search issues https://github.com/acme/secret/issues/1",
        ],
    )
    async def test_the_attached_path_still_runs_its_own_references_and_flag_values(self, subcommand):
        with patched_cross_project() as mocks:
            result = await run_gh(subcommand, installation_runtime(), project="", cross_project_enabled=True)

        assert result == "ok"
        mocks.create_proc.assert_called_once()

    async def test_the_cross_project_path_refuses_a_third_repository_before_any_credential(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(
                "issue view https://github.com/acme/secret/issues/1", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_FOREIGN_REFERENCE.format(
            reference="https://github.com/acme/secret/issues/1", target=OTHER
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs == {
            "provider": GitPlatform.GITHUB,
            "target_repo_id": OTHER,
            "outcome": CrossProjectOutcome.DENIED_POLICY,
            "acting_user_id": 7,
        }

    async def test_the_cross_project_path_runs_a_reference_to_its_own_target(self):
        runtime = cross_project_runtime(GitPlatform.GITHUB)
        with patched_cross_project() as mocks:
            result = await run_gh(
                f"issue view https://github.com/{OTHER.upper()}/issues/1",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        mocks.resolve.assert_awaited_once()


class TestAnUnrecordedFetchIsWithheld:
    """The ALLOWED row is what keeps a result to the person who fetched it, so a result without one is not returned."""

    @pytest.mark.parametrize(
        ("platform", "run", "subcommand"),
        [(GitPlatform.GITLAB, run_gl, "project-issue list"), (GitPlatform.GITHUB, run_gh, "issue list")],
    )
    async def test_the_result_is_withheld(self, platform, run, subcommand):
        runtime = cross_project_runtime(platform)
        with patched_cross_project(stdout=TARGET_CONTENT.encode()) as mocks:
            mocks.record.return_value = False
            result = await run(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNRECORDED_CROSS_PROJECT.format(project=OTHER)
        assert TARGET_CONTENT not in result

    @pytest.mark.django_db(transaction=True)
    async def test_a_fetch_in_a_thread_without_a_session_row_is_withheld(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)

        result, _, _ = await _run_gl_audited(
            runtime,
            project=OTHER,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            stdout=TARGET_CONTENT.encode(),
        )

        assert result == REFUSAL_UNRECORDED_CROSS_PROJECT.format(project=OTHER)
        assert TARGET_CONTENT not in result

    @pytest.mark.django_db(transaction=True)
    async def test_a_failed_record_write_reports_false(self, member_user):
        runtime = cross_project_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with patch.object(CrossProjectAccessRecord.objects, "acreate", AsyncMock(side_effect=RuntimeError("db down"))):
            recorded = await _record_cross_project_access(
                runtime,
                provider=GitPlatform.GITLAB,
                target_repo_id=OTHER,
                outcome=CrossProjectOutcome.ALLOWED,
                acting_user_id=member_user.pk,
                person="Ada",
            )

        assert recorded is False
