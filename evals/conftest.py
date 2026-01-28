import pytest_asyncio

from codebase.base import Scope
from codebase.context import set_runtime_ctx


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def runtime_ctx():
    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        yield ctx
