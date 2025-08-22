import pytest

from codebase.context import set_repository_ctx


@pytest.fixture(autouse=True, scope="session")
def repository_ctx():
    with set_repository_ctx(repo_id="srtab/daiv", ref="main") as ctx:
        yield ctx
