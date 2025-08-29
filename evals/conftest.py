import pytest_asyncio

from codebase.context import set_repository_ctx


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def repository_ctx():
    async with set_repository_ctx(repo_id="srtab/daiv", ref="main") as ctx:
        yield ctx
