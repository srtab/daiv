import textwrap


class PathsExtractorPrompts:
    @staticmethod
    def format_system_msg():
        return textwrap.dedent(
            """\
            Act as an exceptional senior software engineer that is specialized in extraction algorithm.
            It's absolutely vital that you completely and correctly execute your task.

            ### Guidelines ###
            - Think out loud step-by-step before you start writing the output.
            - The output must be a list of paths in valid JSON.

            You must complete the task.
            """
        )

    @staticmethod
    def format_default_msg(code_snippet: str):
        return textwrap.dedent(
            """\
            ### Task ###
            Search for filesystem paths on the code snippet below.
            Identify clearly which paths belong to a project and only considers those.
            Ignore external paths, don't include them on the output.

            ### Code Snippet ###
            {code_snippet}
            """
        ).format(code_snippet=code_snippet)
