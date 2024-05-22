import abc
import logging
from pathlib import Path
from typing import Generic, Unpack

from codebase.clients import RepoClient
from codebase.indexers import CodebaseIndex
from codebase.models import FileChange, RepositoryFile

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.agents.prompts import DiffRefactorPrompts, RefactorPrompts
from automation.coders.base import Coder, TInvoke, TInvokeReturn
from automation.coders.tools import CodeActionTools
from automation.coders.typings import MergerRequestRefactorInvoke, RefactorInvoke

logger = logging.getLogger(__name__)

EXCLUDE_FILES = ["pipfile.lock", "package-lock.json", "yarn.lock", "Gemfile.lock", "composer.lock"]


class RefactorCoder(Coder[TInvoke, TInvokeReturn], abc.ABC, Generic[TInvoke, TInvokeReturn]):
    repo_client: RepoClient

    def __init__(self, repo_client: RepoClient):
        self.repo_client = repo_client
        self.codebase_index = CodebaseIndex(repo_client)

    def get_repo_files_prompt(self, repo_file_list: list[RepositoryFile]) -> str:
        """
        Get the content of the files in the repository as a prompt.
        """
        for repo_file in repo_file_list:
            repo_file.content = self.repo_client.get_repository_file(
                repo_file.repo_id, repo_file.file_path, ref=repo_file.ref
            )
        return RefactorPrompts.repository_files_to_str(repo_file_list)


class SimpleRefactorCoder(RefactorCoder[RefactorInvoke, list[FileChange]]):
    def invoke(self, *args, **kwargs: Unpack[RefactorInvoke]) -> list[FileChange]:
        """
        Invoke the coder to generate the original and updated blocks from the user input.
        """
        memory = [
            Message(role="system", content=RefactorPrompts.format_system()),
            Message(
                role="user",
                content=RefactorPrompts.format_files_to_change(self.get_repo_files_prompt(kwargs["files_to_change"])),
            ),
            Message(role="assistant", content="Ok."),
            Message(role="user", content=RefactorPrompts.format_user_prompt(kwargs["prompt"])),
        ]

        if kwargs["changes_example_file"] is not None:
            memory.append(
                Message(
                    role="user",
                    content=RefactorPrompts.format_refactor_example(
                        self.get_repo_files_prompt([kwargs["changes_example_file"]])
                    ),
                )
            )
        code_actions = CodeActionTools(
            self.repo_client, kwargs["files_to_change"][0].repo_id, kwargs["files_to_change"][0].ref
        )
        self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")
        response = self.agent.run()

        if response is None:
            return []

        return list(code_actions.file_changes.values())


class MergeRequestRefactorCoder(RefactorCoder[MergerRequestRefactorInvoke, list[FileChange]]):
    """
    A coder that applies the changes from a merge request diff to a repository.
    """

    def invoke(self, *args, **kwargs: Unpack[MergerRequestRefactorInvoke]) -> list[FileChange]:
        mr_diff_list = self.repo_client.get_merge_request_diff(kwargs["source_repo_id"], kwargs["merge_request_id"])
        code_actions = CodeActionTools(self.repo_client, kwargs["target_repo_id"], kwargs["target_ref"])

        for mr_diff in mr_diff_list:
            if Path(mr_diff.old_path).name.lower() in EXCLUDE_FILES:
                logger.info("File %s is in the exclude list. Skipping.", mr_diff.old_path)
                continue

            repository_file = RepositoryFile(
                repo_id=kwargs["source_repo_id"],
                file_path=mr_diff.old_path,
                content=self.repo_client.get_repository_file(
                    kwargs["source_repo_id"], mr_diff.old_path, ref=mr_diff.ref
                ),
            )
            if (
                not (
                    filepath_to_change := self.codebase_index.get_most_similar_source(
                        kwargs["target_repo_id"], repository_file
                    )
                )
                and not mr_diff.new_file
            ):
                logger.warning('No similar file "%s" was found in the repository.', mr_diff.old_path)
                continue

            if mr_diff.renamed_file:
                code_actions.file_changes[mr_diff.old_path] = FileChange(
                    action="move",
                    file_path=mr_diff.new_path,
                    previous_path=mr_diff.old_path,
                    commit_messages=[f"Renamed file from {mr_diff.old_path} to {mr_diff.new_path}."],
                )
                continue

            if mr_diff.deleted_file:
                code_actions.file_changes[mr_diff.old_path] = FileChange(
                    action="delete", file_path=mr_diff.old_path, commit_messages=["Deleted file {mr_diff.old_path}."]
                )
                continue

            if mr_diff.new_file:
                file_to_change = RepositoryFile(repo_id=kwargs["source_repo_id"], file_path=mr_diff.old_path)
            else:
                file_to_change = RepositoryFile(repo_id=kwargs["target_repo_id"], file_path=filepath_to_change)

            memory = [Message(role="system", content=DiffRefactorPrompts.format_system())]
            if not mr_diff.new_file:
                memory += [
                    Message(
                        role="user",
                        content=DiffRefactorPrompts.format_files_to_change(
                            self.get_repo_files_prompt([file_to_change])
                        ),
                    ),
                    Message(role="assistant", content="Ok."),
                ]
            memory.append(Message(role="user", content=DiffRefactorPrompts.format_diff(mr_diff.diff.decode())))

            self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")
            response = self.agent.run()

            if response is None:
                continue

        return list(code_actions.file_changes.values())


if __name__ == "__main__":
    repo_client = RepoClient.create_instance()

    # coder = SimpleRefactorCoder(repo_client=repo_client)
    # response = coder.invoke(
    #     prompt="Integrate debugpy package to the Django manage.py file.",
    #     files_to_change=[RepositoryFile(repo_id="dipcode/bankinter/app", file_path="docker-compose.yml")],
    #     changes_example_file=RepositoryFile(
    #         repo_id="dipcode/inovretail/brodheim/feed-portal", file_path="docker-compose.yml", ref="release/phase-2"
    #     ),
    # )

    repo_id = "dipcode/bankinter/app"
    coder = MergeRequestRefactorCoder(repo_client=repo_client)
    response = coder.invoke(
        target_repo_id=repo_id,
        target_ref="dev",
        source_repo_id="dipcode/inovretail/brodheim/feed-portal",
        merge_request_id="485",
    )

    repo_client.commit_changes(
        repo_id, "dev", "feat/debugpy", "Integrate debugpy package to the Django manage.py file.", response
    )
    repo_client.get_or_create_merge_request(
        repo_id=repo_id,
        source_branch="feat/debugpy",
        target_branch="dev",
        title="Integrate debugpy package to the Django manage.py file.",
        description="Integrate debugpy package to the Django manage.py file.",
    )
