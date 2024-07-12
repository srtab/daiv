import logging
import textwrap
from logging.config import dictConfig
from pathlib import Path
from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message, Usage
from automation.coders.base import CodebaseCoder
from automation.coders.change_describer.coder import ChangesDescriberCoder
from automation.coders.change_describer.models import ChangesDescriber
from automation.coders.refactor.prompts import PatchRefactorPrompts
from automation.coders.tools import CodeActionTools
from automation.coders.typings import MergerRequestRefactorInvoke
from codebase.base import FileChange, FileChangeAction, RepositoryFile
from codebase.document_loaders import EXCLUDE_PATTERN

logger = logging.getLogger(__name__)


class MergeRequestRefactorCoder(CodebaseCoder[MergerRequestRefactorInvoke, list[FileChange]]):
    """
    A coder that applies the changes from a merge request diff to a repository.
    """

    def invoke(self, *args, **kwargs: Unpack[MergerRequestRefactorInvoke]) -> list[FileChange]:
        """
        Invoke the coder to apply the changes from a merge request diff to a repository.
        """
        mr_diff_list = self.repo_client.get_merge_request_diff(kwargs["source_repo_id"], kwargs["merge_request_id"])
        code_actions = CodeActionTools(
            self.repo_client,
            self.codebase_index,
            self.usage,
            repo_id=kwargs["target_repo_id"],
            ref=kwargs["target_ref"],
            replace_paths=True,
        )

        for mr_diff in mr_diff_list:
            path = Path(mr_diff.old_path)
            if any(path.match(glob, case_sensitive=False) for glob in EXCLUDE_PATTERN):
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
                    action=FileChangeAction.MOVE,
                    previous_path=filepath_to_change,
                    file_path=mr_diff.new_path,
                    commit_messages=[f"Renamed file from {filepath_to_change} to {mr_diff.new_path}."],
                )
                continue

            if mr_diff.deleted_file:
                code_actions.file_changes[mr_diff.old_path] = FileChange(
                    action=FileChangeAction.DELETE,
                    file_path=mr_diff.old_path,
                    commit_messages=["Deleted file {mr_diff.old_path}."],
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

            agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")
            response = agent.run()

            self.usage += agent.usage

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

    usage = Usage()

    repo_id = "dipcode/bankinter/app"
    source_repo_id = "dipcode/inovretail/brodheim/feed-portal"
    merge_request_id = "485"

    coder = MergeRequestRefactorCoder(usage)
    response: list[FileChange] = coder.invoke(
        target_repo_id=repo_id, target_ref="dev", source_repo_id=source_repo_id, merge_request_id=merge_request_id
    )

    changes_description: ChangesDescriber | None = ChangesDescriberCoder(usage).invoke(
        changes=[". ".join(file_change.commit_messages) for file_change in response]
    )

    if changes_description is None:
        logger.error("No changes description was generated.")
        exit(1)

    coder.repo_client.commit_changes(
        repo_id, "dev", changes_description.branch, changes_description.commit_message, response
    )
    coder.repo_client.update_or_create_merge_request(
        repo_id=repo_id,
        source_branch=changes_description.branch,
        target_branch="dev",
        title=changes_description.title,
        description=textwrap.dedent(
            """\
            👋 Hi there! This PR was automatically generated based on {source_repo_id}!{merge_request_id}

            {description}

            ### 📣 Instructions for the reviewer which is you, yes **you**:
            - **If these changes were incorrect, please close this PR and comment explaining why.**
            - **If these changes were incomplete, please continue working on this PR then merge it.**
            - **If you are feeling confident in my changes, please merge this PR.**

            This will greatly help us improve the AequatioAI system. Thank you! 🙏

            ### 🤓 Stats for the nerds:
            Prompt tokens: **{prompt_tokens:,}** \\
            Completion tokens: **{completion_tokens:,}** \\
            Total tokens: **{total_tokens:,}** \\
            Estimated cost: **${total_cost:.10f}**"""
        ).format(
            description=changes_description.description,
            source_repo_id=source_repo_id,
            merge_request_id=merge_request_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            total_cost=usage.cost,
        ),
    )
