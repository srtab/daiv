import textwrap


class ChangesDescriberPrompts:
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
            Provide the information about the changes described below.

            ### Changes ###
            {changes}
            """
        ).format(changes="\n".join(changes))
