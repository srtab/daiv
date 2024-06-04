import textwrap


class ChangeDescriberPrompts:
    @staticmethod
    def format_system_msg():
        return textwrap.dedent(
            """\
            Act as an exceptional senior software engineer that is specialized in describing changes.
            It's absolutely vital that you completely and correctly execute your task.
            """
        )

    @staticmethod
    def format_default_msg(changes: list[str]):
        return textwrap.dedent(
            """\
            ### Task ###
            Provide the following information about the changes below:
            - Branch name.
            - Title for the pull request.
            - Description for the pull request.
            - Commit message, short and concise.

            ### Changes ###
            {changes}

            ### Output ###
            Output must be in valid JSON format.
            Example: ```{{"branch": "", "title": "", "description": "", "commit_message": ""}}```
            """
        ).format(changes="\n".join(changes))
