import textwrap

from codebase.models import RepositoryFile


class ReplacerPrompts:
    @staticmethod
    def format_default_msg(original_snippet: str, replacement_snippet: str, content: str, commit_message: str):
        return textwrap.dedent(
            """\
            Replace the following snippet:

            <snippet>
            {original_snippet}
            </snippet>

            with the following snippet:
            <snippet>
            {replacement_snippet}
            </snippet>

            in the below chunk of code:
            <chunk>
            {content}
            </chunk>

            The intent of this change is
            <description>
            {commit_message}
            </description>

            Make sure you fix any errors in the code and ensure it is working as expected to the intent of the change.
            Do not make extraneous changes to the code or whitespace that are not related to the intent of the change.

            You MUST return the code result inside a <code></code> tag."""
        ).format(
            original_snippet=original_snippet,
            replacement_snippet=replacement_snippet,
            content=content,
            commit_message=commit_message,
        )


class RefactorPrompts:
    @staticmethod
    def format_system():
        """
        Format the system prompt for the task.
        """
        return textwrap.dedent(
            """\
            You are an exceptional senior engineer that is responsible for code refactoring.
            Given the available tools and below task, which corresponds to an important step to code refactoring,
            convert the task into code.
            It's absolutely vital that you completely and correctly execute your task.

            When the task is complete, reply with "<DONE>"
            If you are unable to complete the task, also reply with "<DONE>"

            <guidelines>
            - Only suggest changes to a *read-write* files.
            - Write code by calling the available tools.
            - The code must be valid, executable code.
            - Code padding, spacing, and indentation matters, make sure that the indentation is corrected for.
            - Do not make extraneous changes to the code or whitespace that are not related to the intent of the change.
            </guidelines>"""
        )

    @staticmethod
    def format_user_prompt(prompt: str) -> str:
        """
        Format the user prompt for the task.
        """
        return textwrap.dedent(
            """\
            <task>
            {prompt}
            </task>

            You must complete the task.

            - Think out loud step-by-step before you start writing code.
            - Do not just add a comment or leave a TODO, you must write functional code.
            - Importing libraries and modules should be done in its own step.
            - Carefully review your code and ensure that it is formatted correctly.

            You must use the tools/functions provided to do so."""
        ).format(prompt=prompt)

    @staticmethod
    def format_files_to_change(files_content: str) -> str:
        """
        Format the files to change for the user prompt.
        """
        return textwrap.dedent("These are the code snippets to refactor:\n{files_content}").format(
            files_content=files_content
        )

    @staticmethod
    def format_refactor_example(file_content: str) -> str:
        """
        Format the refactor example for the user prompt.
        """
        return textwrap.dedent("You can use the following code example to make the refactor:\n{content}").format(
            content=file_content
        )

    @staticmethod
    def repository_files_to_str(repository_files: list[RepositoryFile]) -> str:
        """
        Get the content of the files in the repository to be used in a prompt.
        """
        return "\n".join([
            textwrap.dedent(
                """\
                file-path: {file_path}
                <code>
                {content}
                </code>"""
            ).format(file_path=repo_file.file_path, content=repo_file.content)
            for repo_file in repository_files
        ])


class DiffRefactorPrompts(RefactorPrompts):
    @staticmethod
    def format_system():
        """
        Format the system prompt for the task.
        """
        return textwrap.dedent(
            """\
            You are an exceptional senior software engineer that is responsible for code refactoring.
            Given the available tools and below task, which corresponds to an important step to code refactoring,
            convert the task into code.
            It's absolutely vital that you completely and correctly execute your task.

            When the task is complete, reply with "<DONE>"
            If you are unable to complete the task, also reply with "<DONE>"

            <guidelines>
            - Write code by calling the available tools.
            - Ensure the code is valid and executable after applying changes.
            - Maintain correct code padding, spacing, and indentation.
            - Do not make extraneous changes to the code or whitespace that are not related to the intent of the change.
            </guidelines>"""
        )

    @staticmethod
    def format_diff(content: str) -> str:
        return textwrap.dedent(
            """\
            <task>
            - Refactor the code by applying this unified diff.
            - The diff is a patch that contains the changes to be made to the code.
            </task>

            <diff>
            {content}
            </diff>

            You must complete the task.

            - Think out loud step-by-step before you start writing code.
            - Do not just add a comment or leave a TODO; you must write functional code.
            - Import libraries and modules in their own step.
            - Carefully review your code and ensure it is formatted correctly.

            You must use the provided tools/functions to do so."""
        ).format(content=content)
