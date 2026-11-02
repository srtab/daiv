import pytest

from codebase.clients.github.client import github_conclusion_to_status


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", "success"),
        ("neutral", "success"),
        ("failure", "failed"),
        ("timed_out", "failed"),
        ("cancelled", "canceled"),
        ("stale", "canceled"),
        ("skipped", "skipped"),
        ("action_required", "manual"),
    ],
)
def test_github_conclusion_maps_to_domain_status(conclusion, expected):
    assert github_conclusion_to_status(conclusion) == expected


def test_an_unfinished_github_run_is_running():
    assert github_conclusion_to_status(None) == "running"


def test_an_unknown_conclusion_is_not_silently_green():
    # A vocabulary GitHub adds later must not be read as success.
    assert github_conclusion_to_status("something_new") == "canceled"


class TestUnreadableIsNotAbsent:
    """``None`` means "no such pipeline", which ``PipelineReport`` reads as UNCLEAR and the watch
    treats as "nothing to judge yet". A permission or rate-limit failure answering the same way
    makes an outage indistinguishable from a slow CI queue, so those must raise."""

    @staticmethod
    def _github_client(exc):
        from unittest.mock import MagicMock

        from codebase.clients.github.client import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        repo = MagicMock()
        repo.get_workflow_run.side_effect = exc
        client.client = MagicMock()
        client.client.get_repo.return_value = repo
        return client

    def test_github_missing_run_is_absent(self):
        from github import GithubException

        assert self._github_client(GithubException(404, None, None)).get_pipeline("g/r", 1) is None

    @pytest.mark.parametrize("status", [403, 429, 500])
    def test_github_unreadable_run_raises(self, status):
        from github import GithubException

        with pytest.raises(GithubException):
            self._github_client(GithubException(status, None, None)).get_pipeline("g/r", 1)

    @staticmethod
    def _gitlab_client(exc):
        from unittest.mock import MagicMock

        from codebase.clients.gitlab.client import GitLabClient

        client = GitLabClient.__new__(GitLabClient)
        project = MagicMock()
        project.pipelines.get.side_effect = exc
        client.client = MagicMock()
        client.client.projects.get.return_value = project
        return client

    def test_gitlab_missing_pipeline_is_absent(self):
        from gitlab.exceptions import GitlabGetError

        assert self._gitlab_client(GitlabGetError(response_code=404)).get_pipeline("g/r", 1) is None

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_gitlab_unreadable_pipeline_raises(self, status):
        from gitlab.exceptions import GitlabGetError

        with pytest.raises(GitlabGetError):
            self._gitlab_client(GitlabGetError(response_code=status)).get_pipeline("g/r", 1)


class TestTransientPlatformErrors:
    """The watch polls every 10 minutes for up to 6 hours, so an outage classified as a bug mints
    dozens of Sentry errors per repo. Auth counts as transient: DAIV's project-scoped tokens are
    ephemeral and expire mid-watch by design."""

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_platform_outages_are_transient(self, status):
        from github import GithubException
        from gitlab.exceptions import GitlabGetError

        from codebase.clients.base import is_transient_platform_error

        assert is_transient_platform_error(GithubException(status, None, None))
        assert is_transient_platform_error(GitlabGetError(response_code=status))

    def test_a_missing_resource_is_not_transient(self):
        from github import GithubException

        from codebase.clients.base import is_transient_platform_error

        assert not is_transient_platform_error(GithubException(404, None, None))

    def test_a_bug_in_our_own_code_is_not_transient(self):
        from codebase.clients.base import is_transient_platform_error

        assert not is_transient_platform_error(AttributeError("'NoneType' object has no attribute 'sha'"))

    def test_a_dropped_connection_is_transient(self):
        import requests

        from codebase.clients.base import is_transient_platform_error

        assert is_transient_platform_error(requests.ConnectionError("connection refused"))
        assert is_transient_platform_error(OSError("socket closed"))


class TestCiSubjectIdentity:
    """GitHub runs one workflow run per workflow on a commit, so a run names its workflow; GitLab's one pipeline
    covers the whole commit and names none."""

    def test_a_github_run_names_its_workflow(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from codebase.clients.github.client import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        run = SimpleNamespace(
            id=100, head_sha="abc", conclusion="failure", html_url="https://gh/run/100", workflow_id=11, name="Lint",
            jobs=lambda: [SimpleNamespace(id=1, name="ruff", conclusion="failure")],
        )  # fmt: skip
        repo = MagicMock()
        repo.get_workflow_run.return_value = run
        client.client = MagicMock()
        client.client.get_repo.return_value = repo

        pipeline = client.get_pipeline("g/r", 100)

        assert (pipeline.workflow_id, pipeline.workflow_name) == (11, "Lint")

    def test_a_gitlab_pipeline_names_no_workflow(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from codebase.clients.gitlab.client import GitLabClient

        client = GitLabClient.__new__(GitLabClient)
        jobs = MagicMock()
        jobs.list.return_value = [
            SimpleNamespace(id=1, name="lint", status="failed", stage="test", allow_failure=False)
        ]
        pipeline = SimpleNamespace(id=100, iid=3, sha="abc", status="failed", web_url="https://gl/p/100", jobs=jobs)
        project = MagicMock()
        project.pipelines.get.return_value = pipeline
        client.client = MagicMock()
        client.client.projects.get.return_value = project

        result = client.get_pipeline("g/r", 100)

        assert (result.workflow_id, result.workflow_name) == (None, "")
