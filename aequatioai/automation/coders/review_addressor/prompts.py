import textwrap

from automation.coders.base import STOP_MESSAGE


class ReviewAddressorPrompts:
    @staticmethod
    def format_system():
        """
        Format the system prompt for the task.
        """
        from .coder import STOP_MESSAGE_QUESTION

        return textwrap.dedent(
            """\
            ### Instructions ###
            Act as a exceptional senior software engineer that is responsible for writing code.
            Given the available tools and below task, which corresponds to an important step in writing code,
            convert the task into code.
            It's absolutely vital that you completely and correctly execute your task.

            When the task is complete, reply with "{STOP_MESSAGE}".

            If the comments are off-topic or ambiguous, use the tool/function to ask for feedback to help you complete the task.

            ### Guidelines ###
            - Think out loud step-by-step before you start writing code.
            - Write code by calling the available tools.
            - Ensure the code is valid and executable after applying changes.
            - Always use best practices when coding.
            - Maintain correct code padding, spacing, and indentation.
            - Do not make extraneous changes to the code or whitespace that are not related to the intent.
            - Do not just add a comment or leave a TODO; you must write functional code.
            - Import libraries and modules in their own step.
            - Carefully review your code and ensure it respects and use existing conventions, libraries, etc that are already present in the codebase.
            You must use the provided tools/functions to do so.
            """  # noqa: E501
        ).format(STOP_MESSAGE=STOP_MESSAGE, STOP_MESSAGE_QUESTION=STOP_MESSAGE_QUESTION)

    @staticmethod
    def format_review_task_prompt(file_path: str) -> str:
        """
        Format the review feedback for the task.
        """
        return textwrap.dedent(
            """\
            ### Task ###
            I have reviewed the changes made on file {file_path} and I will give you the comments I have left for you to analyse and address with code changes.
            """  # noqa: E501
        ).format(file_path=file_path)

    @staticmethod
    def format_review_hunk_prompt() -> str:
        """ """
        return textwrap.dedent(
            """\
            To help you complete your task, here is the hunk of the unified diff with the changes made and the indication of the lines of code on which I left comments for you to address (multiline comments). The hunk was extracted from the pull request where i left the comments.
            """  # noqa: E501
        )
