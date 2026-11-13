from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from automation.agent.middlewares.git_platform.cross_project import REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT
from automation.agent.middlewares.git_platform.gitlab import REFUSAL_FILE_ARGUMENT, run_gitlab_subcommand
from codebase.base import GitPlatform
from core.constants import CrossProjectOutcome
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import (
    ATTACHED,
    OTHER,
    VALID_POSITION,
    cli_process,
    cross_project_runtime,
    gitlab_runtime,
    patched_cross_project,
    run_gl,
)
from tests.unit_tests.conftest import recording_backend

pytestmark = pytest.mark.usefixtures("gitlab_settings")


async def _run_gl(subcommand, runtime, *, output_mode="simplified", to_file=False, backend=None):
    return await run_gitlab_subcommand(
        subcommand, runtime, output_mode, to_file, backend=backend if backend is not None else recording_backend()
    )


class TestGitLabToolInlineDiscussionFallback:
    """Tests for the python-gitlab CLI workaround that routes inline MR diff discussion
    creation through the RepoClient Python API when --position is supplied."""

    async def test_uses_python_api_when_position_flag_present(self):
        runtime = gitlab_runtime()

        runtime.context.repo.client.create_merge_request_inline_discussion.return_value = "disc-1"

        position_json = json.dumps(VALID_POSITION)
        result = await _run_gl(
            f'project-merge-request-discussion create --mr-iid 10 --body "nice" --position {json.dumps(position_json)}',
            runtime,
        )

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["id"] == "disc-1"
        assert data["status"] == "created"
        runtime.context.repo.client.create_merge_request_inline_discussion.assert_called_once_with(
            "group/repo", 10, "nice", VALID_POSITION
        )

    async def test_uses_python_api_with_position_equals_syntax(self):
        """--position=<value> form must also trigger the fallback.

        Single-quote shell quoting in the subcommand string ensures shlex.split
        keeps the whole JSON value (including spaces) as one token.
        """
        runtime = gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)
        # Wrap with shell single-quotes so shlex.split preserves the JSON as one token.
        subcommand = f"project-merge-request-discussion create --mr-iid 20 --body body '--position={position_json}'"

        runtime.context.repo.client.create_merge_request_inline_discussion.return_value = "disc-eq"

        result = await _run_gl(subcommand, runtime)

        assert json.loads(result)["id"] == "disc-eq"

    async def test_falls_through_to_cli_when_no_position_flag(self):
        """Without --position the CLI subprocess must still be invoked."""
        runtime = gitlab_runtime()

        with cli_process(stdout=b"cli-output\n") as create_proc:
            result = await _run_gl('project-merge-request-discussion create --mr-iid 10 --body "hi"', runtime)

        assert result == "cli-output"
        runtime.context.repo.client.create_merge_request_inline_discussion.assert_not_called()
        create_proc.assert_called_once()

    async def test_error_when_mr_iid_missing(self):
        runtime = gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        result = await _run_gl(
            f'project-merge-request-discussion create --body "b" --position {json.dumps(position_json)}', runtime
        )

        assert result.startswith("error:")
        assert "--mr-iid" in result

    async def test_error_when_body_missing(self):
        runtime = gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        result = await _run_gl(
            f"project-merge-request-discussion create --mr-iid 5 --position {json.dumps(position_json)}", runtime
        )

        assert result.startswith("error:")
        assert "--body" in result

    async def test_error_when_position_is_invalid_json(self):
        runtime = gitlab_runtime()

        result = await _run_gl(
            'project-merge-request-discussion create --mr-iid 5 --body "b" --position "not-json"', runtime
        )

        assert result.startswith("error:")
        assert "--position" in result

    async def test_error_when_position_is_not_an_object(self):
        runtime = gitlab_runtime()

        result = await _run_gl(
            'project-merge-request-discussion create --mr-iid 5 --body "b" --position "[1,2,3]"', runtime
        )

        assert result.startswith("error:")

    async def test_error_propagated_from_repo_client(self):
        runtime = gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        runtime.context.repo.client.create_merge_request_inline_discussion.side_effect = RuntimeError("GitLab 422")

        result = await _run_gl(
            f'project-merge-request-discussion create --mr-iid 10 --body "b" --position {json.dumps(position_json)}',
            runtime,
        )

        assert result.startswith("error:")
        assert "GitLab 422" in result

    @pytest.mark.parametrize(
        "subcommand",
        [
            pytest.param(
                'project-merge-request-discussion create --mr-iid abc --body "b" --position "{}"', id="non-int-iid"
            )
        ],
    )
    async def test_error_when_mr_iid_not_integer(self, subcommand):
        runtime = gitlab_runtime()

        result = await _run_gl(subcommand, runtime)

        assert result.startswith("error:")
        assert "--mr-iid" in result


async def test_gitlab_output_to_file_forces_json_writes_and_confirms():
    runtime = gitlab_runtime()
    backend = recording_backend()

    payload = b'[{"iid": 1}, {"iid": 2}]\n'

    with cli_process(stdout=payload) as create_proc:
        result = await _run_gl(
            "project-merge-request list --state opened", runtime, output_mode="detailed", to_file=True, backend=backend
        )

    argv = list(create_proc.call_args.args)
    assert "--output" in argv and argv[argv.index("--output") + 1] == "json"
    assert "--verbose" not in argv  # output_mode ignored when writing to file

    path, content = backend.awrite.call_args.args
    assert path == "/workspace/large_tool_results/test_call_gitlab"
    assert content == '[{"iid": 1}, {"iid": 2}]'  # full, untruncated
    assert result.startswith("Wrote ")
    assert "/workspace/large_tool_results/test_call_gitlab" in result
    assert '"iid": 1' in result  # head preview


async def test_gitlab_output_to_file_does_not_force_json_for_job_trace():
    runtime = gitlab_runtime()
    backend = recording_backend()
    with cli_process(stdout=b"log line 1\nlog line 2\n") as create_proc:
        result = await _run_gl("project-job trace --id 55", runtime, to_file=True, backend=backend)
    argv = list(create_proc.call_args.args)
    assert "--output" not in argv  # traces are raw log text; JSON would be degenerate
    assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/test_call_gitlab"
    assert result.startswith("Wrote ")


async def test_gitlab_empty_output_to_file_notes_no_file_written():
    """When the gitlab CLI returns empty stdout and output_to_file is true, the result must
    contain both the 'empty result' sentinel and a note that no file was written."""
    runtime = gitlab_runtime()
    backend = recording_backend()

    with cli_process(stdout=b""):
        result = await _run_gl("project-issue list --state opened", runtime, to_file=True, backend=backend)

    assert "empty result" in result
    assert "no file was written" in result
    backend.awrite.assert_not_called()


class TestPublishedTextIsRepoRelative:
    async def test_a_gitlab_note_body_is_repo_relative(self):
        runtime = gitlab_runtime()
        with cli_process(stdout=b"ok\n") as create_proc:
            await _run_gl('project-issue-note create --issue-iid 42 --body "See /workspace/repo/daiv/x.py:3"', runtime)

        argv = list(create_proc.call_args.args)
        assert argv[argv.index("--body") + 1] == "See daiv/x.py:3"

    async def test_inline_discussion_body_is_repo_relative(self):
        runtime = gitlab_runtime()
        runtime.context.repo.client.create_merge_request_inline_discussion.return_value = "disc-1"
        position_json = json.dumps(VALID_POSITION)
        await _run_gl(
            f'project-merge-request-discussion create --mr-iid 10 --body "see /workspace/repo/src/foo.py" '
            f"--position {json.dumps(position_json)}",
            runtime,
        )

        runtime.context.repo.client.create_merge_request_inline_discussion.assert_called_once_with(
            "group/repo", 10, "see src/foo.py", VALID_POSITION
        )


async def test_gitlab_failure_keeps_the_command_output():
    runtime = gitlab_runtime()
    with cli_process(stdout=b"partial output\n", stderr=b"404 Not Found\n", returncode=1):
        result = await _run_gl("project-merge-request get --iid 9", runtime)

    assert result.startswith("error: GitLab command failed (exit code 1). Details: 404 Not Found")
    assert "partial output" in result


class TestIdentitySelection:
    """The service token for the attached project, the person's for any other."""

    async def test_an_empty_project_uses_the_service_token(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl("project-issue list", runtime, project="")
        envs = mocks.create_proc.call_args.kwargs["env"]
        assert envs["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert mocks.create_proc.call_args.args[-1] == ATTACHED
        mocks.resolve.assert_not_called()

    async def test_a_self_referential_project_uses_the_service_token(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl("project-issue list", runtime, project=ATTACHED)
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.resolve.assert_not_called()

    async def test_another_project_uses_the_persons_token(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            await run_gl("project-issue list", runtime, project=OTHER)
        args = mocks.create_proc.call_args.args
        envs = mocks.create_proc.call_args.kwargs["env"]
        # A person's grant is an OAuth token: GitLab resolves PRIVATE-TOKEN against personal
        # access tokens only and 401s anything else, so it has to travel as a bearer.
        assert envs["GITLAB_OAUTH_TOKEN"] == "person-token"  # noqa: S105
        assert "GITLAB_PRIVATE_TOKEN" not in envs
        assert args[-2:] == ("--project-id", OTHER)
        mocks.record.assert_awaited()


class TestAttachedProjectIsUnchanged:
    """An empty ``project`` leaves the attached-project path as it was."""

    async def test_the_attached_path_is_unchanged(self):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with patched_cross_project() as mocks:
            result = await run_gl("project-issue list", runtime, project="")
        assert result == "ok"
        assert mocks.create_proc.call_args.args[-2:] == ("--project-id", ATTACHED)
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.record.assert_not_called()


class TestCrossProjectInlineDiscussionIsRefused:
    """The python-gitlab CLI cannot encode a nested position hash, so inline discussions go
    through RepoClient — which holds the service token, the one identity this path may not use."""

    @pytest.mark.parametrize("flag", ["--position", "--pos"])
    async def test_inline_discussion_on_another_project_is_refused_before_any_credential(self, flag):
        runtime = cross_project_runtime(GitPlatform.GITLAB)
        with (
            patched_cross_project() as mocks,
            patch("automation.agent.middlewares.git_platform.gitlab._create_gitlab_inline_discussion") as inline_mock,
        ):
            subcommand = (
                f"project-merge-request-discussion create --mr-iid 1 --body \"x\" {flag} '{json.dumps(VALID_POSITION)}'"
            )
            result = await run_gl(subcommand, runtime, project=OTHER)
        assert result == REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        inline_mock.assert_not_called()
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY


class TestAValueReadFromAFileIsRefusedEverywhere:
    """python-gitlab reads ``@path`` from the worker's disk, where ``/proc/self/environ`` holds the service token."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue-note create --issue-iid 1 --body @/proc/self/environ",
            "project-issue-note create --issue-iid 1 --body=@/proc/self/environ",
        ],
    )
    async def test_the_attached_project_refuses_it_before_the_cli_runs(self, subcommand):
        with cli_process(stdout=b"ok\n") as create_proc:
            result = await _run_gl(subcommand, gitlab_runtime())

        assert result == REFUSAL_FILE_ARGUMENT
        create_proc.assert_not_called()

    async def test_an_escaped_mention_reaches_an_inline_discussion_unescaped(self):
        runtime = gitlab_runtime()
        runtime.context.repo.client.create_merge_request_inline_discussion.return_value = "disc-1"
        position_json = json.dumps(VALID_POSITION)
        await _run_gl(
            f'project-merge-request-discussion create --mr-iid 10 --body "@@alice see this" '
            f"--position {json.dumps(position_json)}",
            runtime,
        )

        runtime.context.repo.client.create_merge_request_inline_discussion.assert_called_once_with(
            "group/repo", 10, "@alice see this", VALID_POSITION
        )
