import textwrap


class PathsExtractorPrompts:
    @staticmethod
    def format_system_msg():
        return textwrap.dedent(
            """\
            Act as an exceptional senior software engineer that is specialized in extraction algorithm.
            It's absolutely vital that you completely and correctly execute your task.
            """
        )

    @staticmethod
    def format_default_msg(code_snippet: str):
        return textwrap.dedent(
            """\
            ### Task ###
            Search for valid filesystem paths on the code snippet below.
            Identify clearly which paths belong to a project and only considers those.
            Ignore external paths, don't include them on the output.
            If you find a path with a variable, ignore it.

            ### Code Snippet ###
            {code_snippet}

            ### Output ###
            Output must be in valid JSON format.
            Example: ```{{"paths": ["/path/to/file.py", "path/to/dir"]}}```
            """
        ).format(code_snippet=code_snippet)

    @staticmethod
    def format_response_msg(paths: str, similar_paths: dict[str, list[str]]):
        context = ""
        for path in paths:
            context += textwrap.dedent(
                """\
            Here is a tree for "{path}" from the other project to you to choose the most relevant:
            {similar_paths}
            """
            ).format(path=path, similar_paths=similar_paths[path])

        return textwrap.dedent(
            """\
            Now the paths you've extracted need to be replaced with paths from other project.
            {context}

            ### Task ###
            Search for the most relevant path for each one provided.
            Identify clearly which paths will be replaced and the one who will replace it.

            ### Output ###
            Output must be in valid JSON format.
            Example: ```{{"paths": [["/path/to/file_to_replace.py", "/new_path/to/file.py"], ["path/to/dir", "newpath/to/dir"]]}}```
            """  # noqa: E501
        ).format(context=context)
