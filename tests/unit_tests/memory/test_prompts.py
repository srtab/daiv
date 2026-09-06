"""Pins the mustache escaping contract of the memory prompts.

``{{var}}`` is HTML-escaped by chevron (``& < > "``), which mangles transcripts, entry
content and observation content. The three variables that carry that kind of text are
triple-braced; this test stops them being "tidied" back.
"""

import pytest
from memory.prompts import consolidation_human, extraction_human

# Contains every character in chevron's escape set: & < > "
DIRTY = 'run 2>&1 | grep "x" <y> && echo done'


def test_extraction_transcript_is_not_html_escaped():
    rendered = extraction_human.format(repo_id="group/project", status="SUCCESSFUL", transcript=DIRTY).content
    assert DIRTY in rendered, rendered


@pytest.mark.parametrize("variable", ["entries", "observations"])
def test_consolidation_content_is_not_html_escaped(variable):
    values = {"repo_id": "group/project", "entries": "e", "observations": "o"}
    values[variable] = DIRTY
    rendered = consolidation_human.format(**values).content
    assert DIRTY in rendered, rendered
