import difflib
import logging
from typing import Unpack, cast

from automation.agents.agent import LlmAgent
from automation.agents.models import Message, Usage
from automation.coders.base import Coder
from automation.coders.paths_replacer.models import ExtractedPaths, PathsToReplace
from automation.coders.paths_replacer.prompts import PathsReplacerPrompts
from automation.coders.typings import PathsReplacerInvoke

logger = logging.getLogger(__name__)


class PathsReplacerCoder(Coder[PathsReplacerInvoke, str | None]):
    """
    A coder that extracts and replace project paths from a code snippet.
    """

    def invoke(self, *args, **kwargs: Unpack[PathsReplacerInvoke]) -> str:
        """
        Extracts and replaces paths from a code snippet.
        """
        agent = LlmAgent(
            memory=[
                Message(role="system", content=PathsReplacerPrompts.format_system_msg()),
                Message(
                    role="user", content=PathsReplacerPrompts.format_default_msg(code_snippet=kwargs["code_snippet"])
                ),
            ]
        )

        extracted_paths = agent.run(single_iteration=True, response_model=ExtractedPaths)

        self.usage += agent.usage

        if extracted_paths is None:
            return kwargs["code_snippet"]

        extracted_paths = cast(ExtractedPaths, extracted_paths)

        logger.debug("Extracted paths: %s", extracted_paths.paths)

        agent.memory.append(
            Message(
                role="user",
                content=PathsReplacerPrompts.format_response_msg(
                    paths=extracted_paths.paths,
                    similar_paths=self._similar_paths(extracted_paths.paths, kwargs["repository_tree"]),
                ),
            )
        )

        path_replacements = agent.run(single_iteration=True, response_model=PathsToReplace)

        self.usage += agent.usage

        if path_replacements is None:
            return kwargs["code_snippet"]

        path_replacements = cast(PathsToReplace, path_replacements)

        logger.debug("Paths replacers: %s", path_replacements.paths)

        replaced_code_snippet = kwargs["code_snippet"]
        for path in path_replacements.paths:
            replaced_code_snippet = replaced_code_snippet.replace(path.original_path, path.new_path)

        return replaced_code_snippet

    def _similar_paths(self, paths_to_search: list[str], paths_tree: list[str]) -> dict[str, list[str]]:
        similar_paths = {}
        for path in paths_to_search:
            similar_paths[path] = difflib.get_close_matches(path, paths_tree, n=10, cutoff=0)
        return similar_paths


if __name__ == "__main__":
    repository_tree = [
        "data/static",
        "docker/local/static",
        "docker/local/static/start",
        "data/static/.gitkeep",
        "example/static",
        "docker/local/app/start-app",
        "example/static/src",
        "docker/local/pgadmin",
        "example/static/src/app",
        "docker/local/app",
        "example/manage.py",
        "example/stats/managers.py",
        "example/alerts/managers.py",
        "example/service_calls/managers.py",
        "example/stats/tests/test_managers.py",
        "example/accounts/managers.py",
        "example/stats/management",
        "example/alerts/tests/test_managers.py",
        "example/processes/managers.py",
        "example/documents/managers.py",
    ]

    coder = PathsReplacerCoder(Usage())
    response = coder.invoke(
        code_snippet="""#!/bin/sh

set -eu pipefail

pipenv run django-admin compilemessages --ignore=.venv
pipenv run django-admin collectstatic --noinput
pipenv run django-admin migrate --noinput

exec pipenv run python daiv/manage.py runserver_plus 0.0.0.0:8000 --cert-file /home/app/src/data/cert.crt --exclude-pattern daiv/static --nopin
""",  # noqa: E501
        repository_tree=repository_tree,
    )

    print(response)  # noqa: T201
