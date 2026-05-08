import pytest


@pytest.fixture
def bypass_gitignore_check(monkeypatch):
    """Neutralize ``GitManager.is_path_ignored`` for tests whose ``runtime.context.gitrepo``
    is a ``SimpleNamespace`` / ``Mock`` shim rather than a real ``git.Repo``. Opt-in via
    ``pytestmark = pytest.mark.usefixtures("bypass_gitignore_check")`` so tests that use a
    real repo (e.g. ``test_sandbox.py::test_write_file_refused_when_path_is_gitignored``)
    keep the production check active.
    """
    from codebase.utils import GitManager

    monkeypatch.setattr(GitManager, "is_path_ignored", lambda self, path: False)
