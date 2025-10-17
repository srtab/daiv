import pytest_asyncio

from codebase.context import set_runtime_ctx


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def runtime_ctx():
    async with set_runtime_ctx(repo_id="srtab/daiv", ref="main") as ctx:
        yield ctx
