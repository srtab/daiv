"""Pins the mustache escaping contract of the memory prompts.

``{{var}}`` is HTML-escaped by chevron (``& < > "``), which mangles transcripts, memory
documents, entry content and observation content. The variables that carry that kind of
text are triple-braced; this test stops them being "tidied" back.
"""

import pytest
from memory.prompts import consolidation_human, extraction_human

# Contains every character in chevron's escape set: & < > "
DIRTY = 'run 2>&1 | grep "x" <y> && echo done'


def test_extraction_transcript_is_not_html_escaped():
    rendered = extraction_human.format(
        repo_id="group/project", status="SUCCESSFUL", transcript=DIRTY, memory=""
    ).content
    assert DIRTY in rendered, rendered


def test_extraction_renders_the_memory_block_when_there_is_memory():
    rendered = extraction_human.format(
        repo_id="group/project", status="SUCCESSFUL", transcript="[ai] x", memory="## Build & test\n- a fact"
    ).content

    assert "- a fact" in rendered
    assert "already records" in rendered


def test_extraction_omits_the_memory_block_when_there_is_no_memory():
    """No fallback line either: an empty memory renders the prompt with nothing added, so a
    cold-start run reads exactly as it did before this feature existed."""
    rendered = extraction_human.format(
        repo_id="group/project", status="SUCCESSFUL", transcript="[ai] x", memory=""
    ).content

    assert "already records" not in rendered
    assert "RE-VERIFIED" not in rendered


def test_extraction_memory_is_not_html_escaped():
    rendered = extraction_human.format(
        repo_id="group/project", status="SUCCESSFUL", transcript="[ai] x", memory=DIRTY
    ).content

    assert DIRTY in rendered


@pytest.mark.parametrize("variable", ["entries", "observations"])
def test_consolidation_content_is_not_html_escaped(variable):
    values = {"repo_id": "group/project", "entries": "e", "observations": "o"}
    values[variable] = DIRTY
    rendered = consolidation_human.format(**values).content
    assert DIRTY in rendered, rendered
