import abc
import logging
import re
from difflib import get_close_matches
from typing import Generic, Unpack

from decouple import config

from aequatioai.automation.agents.agent import LlmAgent
from aequatioai.automation.agents.models import Message
from aequatioai.automation.agents.prompts import SimpleRefactorPrompts
from aequatioai.automation.coders.base import Coder, TInvoke, TInvokeReturn
from aequatioai.automation.coders.replacer import ReplacerCoder
from aequatioai.automation.coders.tools import CodeActionTools
from aequatioai.automation.coders.typings import MergerRequestRefactorInvoke, RefactorInvoke
from aequatioai.codebase.clients import GitLabClient, RepoClient
from aequatioai.codebase.models import FileChange, RepositoryFile

logger = logging.getLogger(__name__)

HEAD = "<<<<<<< SEARCH"
DIVIDER = "======="
UPDATED = ">>>>>>> REPLACE"
EXCLUDE_FILES = ["pipfile.lock", "package-lock.json", "yarn.lock", "Gemfile.lock", "composer.lock"]

separators = "|".join([HEAD, DIVIDER, UPDATED])

split_re = re.compile(r"^((?:" + separators + r")[ ]*\n)", re.MULTILINE | re.DOTALL)

missing_filename_err = (
    "Bad/missing filename. The filename must be alone on the line before the opening fence" " {fence[0]}"
)


class RefactorCoder(Coder[TInvoke, TInvokeReturn], abc.ABC, Generic[TInvoke, TInvokeReturn]):
    repo_client: RepoClient

    fence = "<code>", "</code>"

    def __init__(self, repo_client: RepoClient):
        self.repo_client = repo_client


class SimpleRefactorCoder(RefactorCoder[RefactorInvoke, list[FileChange]]):
    def invoke(self, *args, **kwargs: Unpack[RefactorInvoke]) -> list[FileChange]:
        """
        Invoke the coder to generate the original and updated blocks from the user input.
        """
        # memory = [
        #     Message(
        #         role="system",
        #         content=EditBlockPrompts.format_main_system(fence=self.fence),
        #     ),
        #     Message(
        #         role="user",
        #         content=EditBlockPrompts.format_files_to_change(
        #             files_content=self.get_repo_files_prompt(kwargs["files_to_change"])
        #         ),
        #     ),
        #     Message(role="assistant", content="Ok."),
        #     Message(role="user", content=kwargs["prompt"]),
        # ]
        # if kwargs["changes_example_file"] is not None:
        #     memory.append(
        #         Message(
        #             role="user",
        #             content=EditBlockPrompts.format_refactor_example(
        #                 self.get_repo_files_prompt([kwargs["changes_example_file"]])
        #             ),
        #         )
        #     )

        memory = [
            Message(role="system", content=SimpleRefactorPrompts.format_system()),
            Message(
                role="user",
                content=SimpleRefactorPrompts.format_files_to_change(
                    files_content=self.get_repo_files_prompt(kwargs["files_to_change"])
                ),
            ),
            Message(role="assistant", content="Ok."),
            Message(role="user", content=SimpleRefactorPrompts.format_user_prompt(kwargs["prompt"])),
        ]

        if kwargs["changes_example_file"] is not None:
            memory.append(
                Message(
                    role="user",
                    content=SimpleRefactorPrompts.format_refactor_example(
                        self.get_repo_files_prompt([kwargs["changes_example_file"]])
                    ),
                )
            )

        self.agent = LlmAgent(
            memory=memory,
            tools=CodeActionTools(
                self.repo_client, kwargs["files_to_change"][0].repo_id, kwargs["files_to_change"][0].ref
            ).get_tools(),
            stop_message="<DONE>",
        )
        response = self.agent.run()

        if response is None:
            return []
        return []

    def apply_changes(self, prompt: str, response: str, files_to_change: list[RepositoryFile]) -> list[FileChange]:
        files_to_change_dict: dict[str, RepositoryFile] = {
            file_to_change.file_path: file_to_change for file_to_change in files_to_change
        }
        file_changes: dict[str, FileChange] = {}
        for file_path, original_text, updated_text in self.find_original_update_blocks(response):
            if file_path in file_changes:
                content_to_update = file_changes[file_path].content
            else:
                content_to_update = files_to_change_dict[file_path].content

            file_updated_content = ReplacerCoder().invoke(
                replacement_snippet=original_text,
                reference_snippet=updated_text,
                content=content_to_update,
                commit_message=prompt,
            )

            if file_path in file_changes:
                file_changes[file_path].content = file_updated_content
            else:
                file_changes[file_path] = FileChange(action="update", file_path=file_path, content=file_updated_content)

        return list(file_changes.values())

    def get_repo_files_prompt(self, repo_file_list: list[RepositoryFile]) -> str:
        """
        Get the content of the files in the repository as a prompt.
        """
        prompt = ""
        for repo_file in repo_file_list:
            repo_file.content = self.repo_client.get_repository_file(
                repo_file.repo_id, repo_file.file_path, ref=repo_file.ref
            ).decode()
            prompt += f"\n{repo_file.file_path}\n{self.fence[0]}\n{repo_file.content}{self.fence[1]}\n"
        return prompt

    def find_original_update_blocks(self, content: str):
        """
        Find the original and updated blocks in the content.
        """
        # make sure we end with a newline, otherwise the regex will miss <<UPD on the last line
        if not content.endswith("\n"):
            content = content + "\n"

        pieces = re.split(split_re, content)

        pieces.reverse()
        processed = []

        # Keep using the same filename in cases where GPT produces an edit block without a filename.
        current_filename = None
        try:
            while pieces:
                cur = pieces.pop()

                if cur in (DIVIDER, UPDATED):
                    processed.append(cur)
                    raise ValueError(f"Unexpected {cur}")

                if cur.strip() != HEAD:
                    processed.append(cur)
                    continue

                processed.append(cur)  # original_marker

                filename = self.strip_filename(processed[-2].splitlines()[-1])
                try:
                    if not filename:
                        filename = self.strip_filename(processed[-2].splitlines()[-2])
                    if not filename:
                        if current_filename:
                            filename = current_filename
                        else:
                            raise ValueError(missing_filename_err.format(fence=self.fence))
                except IndexError:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(missing_filename_err.format(fence=self.fence)) from None

                current_filename = filename

                original_text = pieces.pop()
                processed.append(original_text)

                divider_marker = pieces.pop()
                processed.append(divider_marker)
                if divider_marker.strip() != DIVIDER:
                    raise ValueError(f"Expected `{DIVIDER}` not {divider_marker.strip()}")

                updated_text = pieces.pop()
                processed.append(updated_text)

                updated_marker = pieces.pop()
                processed.append(updated_marker)
                if updated_marker.strip() != UPDATED:
                    raise ValueError(f"Expected `{UPDATED}` not `{updated_marker.strip()}")

                yield filename, original_text, updated_text
        except ValueError as e:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ {e.args[0]}") from None
        except IndexError:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ Incomplete SEARCH/REPLACE block.") from None
        except Exception as e:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ Error parsing SEARCH/REPLACE block.") from e

    def strip_filename(self, filename):
        filename = filename.strip()

        if filename == "...":
            return

        if filename.startswith(self.fence[0]):
            return

        filename = filename.rstrip(":")
        filename = filename.strip("`")

        return filename


class MergeRequestRefactorCoder(RefactorCoder[MergerRequestRefactorInvoke, None]):
    """
    A coder that applies the changes from a merge request diff to a repository.
    """

    def invoke(self, *args, **kwargs: Unpack[MergerRequestRefactorInvoke]) -> None:
        mr_diff_list = self.repo_client.get_merge_request_diff(kwargs["repo_id"], kwargs["merge_request_id"])
        repo_tree = self.repo_client.get_repository_tree(kwargs["repo_id"])

        for mr_diff in mr_diff_list:
            filepath_to_change = get_close_matches(mr_diff.old_path, repo_tree, n=1, cutoff=0.6)
            if not filepath_to_change:
                logger.warning('No similar file "%s" was found in the repository.', mr_diff.old_path)
                continue

            print(filepath_to_change)

            # file_to_change = RepositoryFile(repo_id=merge_request.repo_id, file_path=filepath_to_change[0])

            # if Path(file_to_change.file_path).name in EXCLUDE_FILES or mr_diff.new_file:
            #     logger.info(
            #         "File %s is in the exclude list. Skipping.", mr_diff.old_path
            #     )
            #     continue

            # memory = [
            #     Message(
            #         role="system",
            #         content=EditBlockPrompts.format_main_system(fence=self.fence),
            #     ),
            #     Message(
            #         role="user",
            #         content=EditBlockPrompts.format_files_to_change(
            #             files_content=self.get_repo_files_prompt([file_to_change])
            #         ),
            #     ),
            #     Message(role="assistant", content="Ok."),
            #     Message(role="user", content=prompt),
            #     Message(
            #         role="user",
            #         content=EditBlockPrompts.format_diff(mr_diff.diff),
            #     ),
            # ]

            # self.agent = LlmAgent(memory=memory)
            # response = self.agent.run()

            # for (
            #     filename,
            #     original_text,
            #     updated_text,
            # ) in self.find_original_update_blocks(response):
            #     print("## filename:", filename)
            #     print("## original_text:\n", original_text)
            #     print("## updated_text:\n", updated_text)


if __name__ == "__main__":
    coder = SimpleRefactorCoder(
        repo_client=GitLabClient(
            url="https://git.eurotux.com/", auth_token=config("GITLAB_TOKEN")
        )
    )

    response = coder.invoke(
        prompt="Integrate debugpy package to the Django manage.py file.",
        files_to_change=[RepositoryFile(repo_id="dipcode/bankinter/app", file_path="docker-compose.yml")],
        changes_example_file=RepositoryFile(
            repo_id="dipcode/inovretail/brodheim/feed-portal", file_path="docker-compose.yml", ref="release/phase-2"
        ),
    )
    for file_change in response:
        print(file_change.file_path)
        print(file_change.content)
        print("##")

    # coder = MergeRequestRefactorCoder(
    #     repo_client=GitLabClient(
    #         url="https://git.eurotux.com/",
    #         auth_token=config("GITLAB_TOKEN"),
    #     )
    # )

    # coder.invoke(
    #     prompt="Apply diff changes.",
    #     repo_id="dipcode/bankinter/app",
    #     merge_request=MergeRequest(
    #         repo_id="dipcode/inovretail/brodheim/feed-portal",
    #         merge_request_id="485",
    #     ),
    # )
