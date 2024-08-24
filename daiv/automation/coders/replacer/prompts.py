import textwrap


class ReplacerPrompts:
    @staticmethod
    def format_system_msg():
        return textwrap.dedent(
            """\
            Act as an exceptional senior software engineer that is responsible for code snippet replacement.
            It's absolutely vital that you completely and correctly execute your task.

            ### Guidelines ###
            - Ensure the code is valid and executable after applying replacement.
            - Maintain correct code padding, spacing, and indentation.
            - Do not make extraneous changes to the code or whitespace that are not related to the intent.
            - Do not just add a comment or leave a TODO; you must write functional code.
            - Carefully review your code and ensure it is formatted correctly.

            You must complete the task.
            """
        )

    @staticmethod
    def format_default_msg(original_snippet: str, replacement_snippet: str, content: str):
        return textwrap.dedent(
            """\
            ### Tasks ###
            - Find the "original snippet" in the "code snippet" and replace it with the "replacement snippet".

            ### Original snippet ###
            {original_snippet}

            ### Replacement snippet ###
            {replacement_snippet}

            ### Code snippet  ###
            {content}

            ### Output ###
            You MUST return the code result inside a <code></code> tag.
            """  # noqa: E501
        ).format(original_snippet=original_snippet, replacement_snippet=replacement_snippet, content=content)
