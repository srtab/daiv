import logging
from logging.config import dictConfig
from pathlib import Path
from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.refactor import RefactorCoder
from automation.coders.refactor.prompts import PatchRefactorPrompts
from automation.coders.refactor.tools import CodeActionTools
from automation.coders.typings import MergerRequestRefactorInvoke
from codebase.clients import RepoClient
from codebase.models import FileChange, RepositoryFile

logger = logging.getLogger(__name__)

EXCLUDE_FILES = ["pipfile.lock", "package-lock.json", "yarn.lock", "Gemfile.lock", "composer.lock"]


class MergeRequestRefactorCoder(RefactorCoder[MergerRequestRefactorInvoke, list[FileChange]]):
    """
    A coder that applies the changes from a merge request diff to a repository.
    """

    def invoke(self, *args, **kwargs: Unpack[MergerRequestRefactorInvoke]) -> list[FileChange]:
        """
        Invoke the coder to apply the changes from a merge request diff to a repository.
        """
        mr_diff_list = self.repo_client.get_merge_request_diff(kwargs["source_repo_id"], kwargs["merge_request_id"])
        code_actions = CodeActionTools(self.repo_client, kwargs["target_repo_id"], kwargs["target_ref"])

        for mr_diff in mr_diff_list:
            if Path(mr_diff.old_path).name.lower() in EXCLUDE_FILES:
                logger.info("File %s is in the exclude list. Skipping.", mr_diff.old_path)
                continue

            repository_file = RepositoryFile.load_from_repo(
                repo_client=self.repo_client,
                repo_id=kwargs["source_repo_id"],
                file_path=mr_diff.old_path,
                ref=mr_diff.ref,
            )
            if (
                not (
                    filepath_to_change := self.codebase_index.search_most_similar_filepath(
                        kwargs["target_repo_id"], repository_file
                    )
                )
                and not mr_diff.new_file
            ):
                logger.warning(
                    'No similar file "%s" was found in repo "%s".', mr_diff.old_path, kwargs["target_repo_id"]
                )
                continue

            if mr_diff.renamed_file:
                # TODO: The filepaths can be different in the source and target repositories. We need to handle this.
                code_actions.file_changes[mr_diff.old_path] = FileChange(
                    action="move",
                    previous_path=filepath_to_change,
                    file_path=mr_diff.new_path,
                    commit_messages=[f"Renamed file from {filepath_to_change} to {mr_diff.new_path}."],
                )
                continue

            if mr_diff.deleted_file:
                code_actions.file_changes[mr_diff.old_path] = FileChange(
                    action="delete", file_path=mr_diff.old_path, commit_messages=["Deleted file {mr_diff.old_path}."]
                )
                continue

            if mr_diff.new_file:
                file_to_change = RepositoryFile.load_from_repo(
                    repo_client=self.repo_client, repo_id=kwargs["source_repo_id"], file_path=mr_diff.old_path
                )
            else:
                file_to_change = RepositoryFile.load_from_repo(
                    repo_client=self.repo_client, repo_id=kwargs["target_repo_id"], file_path=filepath_to_change
                )

            if file_to_change.content is None:
                logger.warning(
                    'No content found for file "%s" from repo "%s". Skipping.',
                    file_to_change.file_path,
                    file_to_change.repo_id,
                )
                continue

            memory = [Message(role="system", content=PatchRefactorPrompts.format_system())]
            if not mr_diff.new_file:
                memory += [
                    Message(
                        role="user",
                        content=PatchRefactorPrompts.format_code_to_refactor(
                            file_to_change.file_path, file_to_change.content
                        ),
                    ),
                    Message(role="assistant", content="Ok."),
                ]
            memory.append(Message(role="user", content=PatchRefactorPrompts.format_diff(mr_diff.diff.decode())))

            self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")
            response = self.agent.run()

            if response is None:
                continue

        return list(code_actions.file_changes.values())


if __name__ == "__main__":
    dictConfig({
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "verbose": {
                "format": "[%(asctime)s] %(levelname)s - %(name)s - %(message)s",
                "datefmt": "%d-%m-%Y:%H:%M:%S %z",
            }
        },
        "handlers": {"console": {"level": "DEBUG", "class": "logging.StreamHandler", "formatter": "verbose"}},
        "loggers": {
            "": {"level": "INFO", "handlers": ["console"]},
            "automation": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
            "codebase": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
            "gitlab": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
        },
    })

    repo_client = RepoClient.create_instance()

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
