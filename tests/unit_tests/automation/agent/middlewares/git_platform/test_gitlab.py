from __future__ import annotations

import json

import pytest

from automation.agent.middlewares.git_platform.gitlab import run_gitlab_subcommand
from tests.unit_tests.automation.agent.middlewares.git_platform.conftest import cli_process, gitlab_runtime
from tests.unit_tests.conftest import recording_backend

pytestmark = pytest.mark.usefixtures("gitlab_settings")


async def _run_gl(subcommand, runtime, *, output_mode="simplified", to_file=False, backend=None):
    return await run_gitlab_subcommand(
        subcommand, runtime, output_mode, to_file, backend=backend if backend is not None else recording_backend()
    )


VALID_POSITION = {
    "position_type": "text",
    "base_sha": "aaa",
    "start_sha": "bbb",
    "head_sha": "ccc",
    "old_path": "src/foo.py",
    "new_path": "src/foo.py",
    "new_line": 42,
}


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
