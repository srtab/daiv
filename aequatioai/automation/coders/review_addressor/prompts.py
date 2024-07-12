import textwrap


class ReviewAddressorPrompts:
    @staticmethod
    def format_system():
        """
        Format the system prompt for the task.
        """
        return textwrap.dedent(
            """\
            ### Instructions ###
            Act as a exceptional senior software engineer that is responsible for writing code.
            Given the available tools and below task, which corresponds to an important step in writing code,
            convert the task into code.
            It's absolutely vital that you completely and correctly execute your task.

            When the task is complete, reply with "<DONE>"
            If you are unable to complete the task, also reply with "<DONE>"

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
        )

    @staticmethod
    def format_file_review_feedback_prompt(file_path: str, comments: list[str]) -> str:
        """
        Format the review feedback for the task.
        """
        return textwrap.dedent(
            """\
            ### Tasks ###
            A developer has reviewed the file {file_path} and left comments that you need to analyze and apply changes.

            ### Developer Comments ###
            {comments}
            """
        ).format(file_path=file_path, comments="\n".join(comments))

    @staticmethod
    def format_diff_review_feedback_prompt(file_path: str, comments: list[tuple[str, str]]) -> str:
        """
        Format the review feedback for the task.
        """
        diff_content = ""
        for comment in comments:
            diff_content += f"\n{comment[0]}Hunk:\n{comment[1]}\n\n"

        return textwrap.dedent(
            """\
            ### Tasks ###
            A developer has reviewed the file {file_path} and left notes that you need to analyse and address one by one.
            The review below contain a hunk of a unified diff, which includes the line number of the code where the developer left the review to locate you, and the corresponding notes to you address.

            ### Developer Review ###
            {diff_content}
            """  # noqa: E501
        ).format(file_path=file_path, diff_content=diff_content)
