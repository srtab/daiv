"""Pins the mustache escaping contract of the memory prompts.

``{{var}}`` is HTML-escaped by chevron (``& < > "``), which mangles transcripts, memory
documents, entry content and observation content. The variables that carry that kind of
text are triple-braced; this test stops them being "tidied" back.
"""

import pytest
from memory.prompts import (
    CONSOLIDATION_FEW_SHOTS,
    EXTRACTION_FEW_SHOTS,
    consolidation_human,
    consolidation_system,
    extraction_human,
    extraction_system,
)

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


def test_extraction_human_prompt_is_byte_identical_to_pre_memory_baseline_when_there_is_no_memory():
    """Pins the property Fix 1's measurement depends on: a repo with no memory yet must see
    exactly the human message the locked baseline measured, not that message plus a stray blank
    line left over from the removed ``{{^memory}}`` fallback — otherwise cases 001-009 (none of
    which carry a ``memory`` field) would be running against an unmeasured human message.

    Scoped to the human message deliberately: Fix 2 adds few-shots to ``extraction_system``, so
    the FULL prompt sent to the model is no longer byte-identical to what the baseline measured
    for any case — only this human half still is.
    """
    rendered = extraction_human.format(
        repo_id="group/project", status="SUCCESSFUL", transcript="[ai] x", memory=""
    ).content

    assert rendered == (
        "Repository: group/project\n"
        "Run finished with status: SUCCESSFUL\n"
        "\n"
        "Run transcript (roles, text, tool calls; long outputs truncated):\n"
        "~~~\n"
        "[ai] x\n"
        "~~~\n"
        "\n"
        "Extract the observations worth remembering for future runs on this repository.\n"
        "Return an empty list if there are none."
    )


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


def test_extraction_system_includes_the_few_shot_examples():
    """The eval's leak guard reads ``extraction_system``'s own rendered text, not a separate
    registry — so nothing else pins that the few-shots are actually part of what a run is sent."""
    assert EXTRACTION_FEW_SHOTS in extraction_system.format().content


def test_consolidation_system_includes_the_few_shot_examples():
    assert CONSOLIDATION_FEW_SHOTS in consolidation_system.format().content
