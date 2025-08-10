import pytest

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        index = CodebaseIndex(RepoClient.create_instance())
        index.update("srtab/daiv", "main")
        yield
