import difflib
import logging
from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import Coder
from automation.coders.paths_replacer.prompts import PathsExtractorPrompts
from automation.coders.typings import PathsExtractorInvoke

logger = logging.getLogger(__name__)


class PathsReplacerCoder(Coder[PathsExtractorInvoke, str | None]):
    """
    A coder that extracts and replace project paths from a code snippet.
    """

    def invoke(self, *args, **kwargs: Unpack[PathsExtractorInvoke]) -> str:
        """
        Extracts and replaces paths from a code snippet.
        """
        agent = LlmAgent[dict](
            memory=[
                Message(role="system", content=PathsExtractorPrompts.format_system_msg()),
                Message(
                    role="user", content=PathsExtractorPrompts.format_default_msg(code_snippet=kwargs["code_snippet"])
                ),
            ],
            response_format="json",
        )

        extracted_paths = agent.run(single_iteration=True)

        self.usage += agent.usage

        if extracted_paths is None:
            return kwargs["code_snippet"]

        if "paths" not in extracted_paths:
            logger.warning("No paths exctracted.")
            return kwargs["code_snippet"]

        logger.debug("Extracted paths: %s", extracted_paths["paths"])

        agent.memory.append(
            Message(
                role="user",
                content=PathsExtractorPrompts.format_response_msg(
                    paths=extracted_paths["paths"],
                    similar_paths=self._similar_paths(extracted_paths["paths"], kwargs["repository_tree"]),
                ),
            )
        )

        paths_replacers = agent.run(single_iteration=True)

        self.usage += agent.usage

        if paths_replacers is None:
            return kwargs["code_snippet"]

        if "paths" not in paths_replacers:
            logger.warning("No replacers found for paths.")
            return kwargs["code_snippet"]

        logger.debug("Paths replacers: %s", paths_replacers["paths"])

        replaced_code_snippet = kwargs["code_snippet"]
        for original_path, replacement_path in paths_replacers["paths"]:
            replaced_code_snippet = replaced_code_snippet.replace(original_path, replacement_path)

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
        "bkcf_onboarding/static",
        "docker/local/app/start-app",
        "bkcf_onboarding/static/src",
        "docker/local/pgadmin",
        "bkcf_onboarding/static/src/app",
        "docker/local/app",
        "bkcf_onboarding/manage.py",
        "bkcf_onboarding/stats/managers.py",
        "bkcf_onboarding/alerts/managers.py",
        "bkcf_onboarding/service_calls/managers.py",
        "bkcf_onboarding/stats/tests/test_managers.py",
        "bkcf_onboarding/accounts/managers.py",
        "bkcf_onboarding/stats/management",
        "bkcf_onboarding/alerts/tests/test_managers.py",
        "bkcf_onboarding/processes/managers.py",
        "bkcf_onboarding/documents/managers.py",
    ]

    coder = PathsReplacerCoder()
    response = coder.invoke(
        code_snippet="""#!/bin/sh

set -eu pipefail

pipenv run django-admin compilemessages --ignore=.venv
pipenv run django-admin collectstatic --noinput
pipenv run django-admin migrate --noinput

exec pipenv run python feedportal/manage.py runserver_plus 0.0.0.0:8000 --cert-file /home/app/src/data/cert.crt --exclude-pattern feedportal/static --nopin
""",  # noqa: E501
        repository_tree=repository_tree,
    )
