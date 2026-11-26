from pathlib import Path

import pytest
from dashboard import landing

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("path", ["README.md", "docs/index.md", "mkdocs.yml"])
def test_definition_is_repeated_word_for_word(path):
    text = " ".join((REPO_ROOT / path).read_text(encoding="utf-8").split())

    assert landing.DEFINITION.removeprefix("DAIV ") in text
