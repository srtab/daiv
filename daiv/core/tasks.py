import base64
import io
import tarfile
import uuid

import httpx
from celery import shared_task
from pydantic import Base64Bytes, BaseModel

from codebase.base import FileChange, FileChangeAction
from codebase.clients import RepoClient
from core.conf import settings


class RunResult(BaseModel):
    command: str
    output: str
    exit_code: int


class RunCommandResponse(BaseModel):
    results: list[RunResult]
    archive: Base64Bytes | None


@shared_task
def run_sandbox_commands(repo_id: str, ref: str, base_image: str, commands: list[str]):
    repo_client = RepoClient.create_instance()
    project = repo_client.client.projects.get(repo_id)

    tarstream = io.BytesIO()
    project.repository_archive(sha=ref, streamed=True, action=tarstream.write)

    tarstream.seek(0)

    with tarfile.open(fileobj=tarstream, mode="r:*") as tar:
        # GitLab returns a tar archive with the root folder name, so we need to
        # extract the first level folder name to use it as the base workdir.
        first_level_folders = {member.name.split("/")[0] for member in tar.getmembers() if member.isdir()}
        if len(first_level_folders) != 1:
            raise ValueError("Unexpected number of first level folders in the archive.")
        workdir = first_level_folders.pop()

    response = httpx.post(
        f"{settings.DAIV_SANDBOX_URL}/run/commands/",
        json={
            "run_id": str(uuid.uuid4()),
            "base_image": base_image,
            "commands": commands,
            "workdir": workdir,
            "archive": base64.b64encode(tarstream.getvalue()).decode(),
        },
        headers={"X-API-KEY": settings.DAIV_SANDBOX_API_KEY},
        timeout=settings.DAIV_SANDBOX_TIMEOUT,
    )
    response.raise_for_status()
    resp = RunCommandResponse(**response.json())
    if resp.archive:
        file_changes = []
        with io.BytesIO(resp.archive) as archive, tarfile.open(fileobj=archive) as tar:
            for member in tar.getmembers():
                file_changes.append(
                    FileChange(
                        file_path=member.name,
                        action=FileChangeAction.UPDATE,
                        content=tar.extractfile(member).read().decode(),
                    )
                )
        repo_client.commit_changes(repo_id, ref, "Run commands", file_changes)
