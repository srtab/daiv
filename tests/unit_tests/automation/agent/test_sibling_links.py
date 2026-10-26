from unittest.mock import Mock

from automation.agent.sibling_links import (
    RELATED_END,
    RELATED_START,
    RelatedMergeRequest,
    aensure_related_links,
    render_related_block,
    with_related_block,
)
from codebase.base import MergeRequest, User


def rel(slug: str, *, title: str = "T", url: str = "u", description: str = "body", iid: int = 1) -> RelatedMergeRequest:
    merge_request = MergeRequest(
        repo_id=slug,
        merge_request_id=iid,
        source_branch="feat",
        target_branch="main",
        title=title,
        description=description,
        author=User(id=1, username="daiv"),
        draft=False,
        web_url=url,
    )
    client = Mock()
    client.get_merge_request.return_value = merge_request
    return RelatedMergeRequest(client=client, repo_slug=slug, merge_request=merge_request)


def test_render_related_block():
    assert render_related_block([rel("g/mod", title="Fix X", url="https://gl/g/mod/-/merge_requests/3")]) == (
        f"{RELATED_START}\n**Related merge requests** (same DAIV session):\n\n"
        f"- `g/mod`: [Fix X](https://gl/g/mod/-/merge_requests/3)\n{RELATED_END}"
    )


def test_with_related_block_replaces_existing_block_once():
    old = f"intro\n\n{RELATED_START}\nold\n{RELATED_END}\n\noutro"
    new = f"{RELATED_START}\nnew \\1\n{RELATED_END}"

    assert with_related_block(old, new) == f"intro\n\n{new}\n\noutro"


def test_with_related_block_appends_when_absent():
    block = f"{RELATED_START}\nx\n{RELATED_END}"

    assert with_related_block("body\n", block) == f"body\n\n{block}"
    assert with_related_block("", block) == block


async def test_ensure_updates_each_mr_with_the_others():
    first, second = rel("g/app", title="A", url="ua", iid=1), rel("g/mod", title="B", url="ub", iid=2)

    await aensure_related_links([first, second])

    first.client.update_merge_request.assert_called_once_with(
        "g/app", 1, description=with_related_block("body", render_related_block([second]))
    )
    second.client.update_merge_request.assert_called_once_with(
        "g/mod", 2, description=with_related_block("body", render_related_block([first]))
    )


async def test_ensure_skips_unchanged_descriptions():
    other = rel("g/mod", iid=2)
    current = rel("g/app", description=with_related_block("body", render_related_block([other])))

    await aensure_related_links([current, other])

    current.client.update_merge_request.assert_not_called()
    other.client.update_merge_request.assert_called_once()


async def test_ensure_noop_below_two():
    only = rel("g/app")

    await aensure_related_links([only])
    await aensure_related_links([])

    only.client.update_merge_request.assert_not_called()


async def test_ensure_survives_one_failed_update(caplog):
    first, second = rel("g/app", iid=1), rel("g/mod", iid=2)
    first.client.update_merge_request.side_effect = RuntimeError("boom")

    await aensure_related_links([first, second])

    second.client.update_merge_request.assert_called_once()
    assert "Could not link g/app" in caplog.text


async def test_ensure_builds_the_update_from_the_current_remote_description():
    first, second = rel("g/app", description="stale", iid=1), rel("g/mod", iid=2)
    first.client.get_merge_request.return_value = first.merge_request.model_copy(
        update={"description": "edited by a reviewer"}
    )

    await aensure_related_links([first, second])

    first.client.update_merge_request.assert_called_once_with(
        "g/app", 1, description=with_related_block("edited by a reviewer", render_related_block([second]))
    )


async def test_ensure_skips_when_the_current_description_already_has_the_block():
    other = rel("g/mod", iid=2)
    current = rel("g/app", description="stale")
    current.client.get_merge_request.return_value = current.merge_request.model_copy(
        update={"description": with_related_block("body", render_related_block([other]))}
    )

    await aensure_related_links([current, other])

    current.client.update_merge_request.assert_not_called()


async def test_ensure_survives_one_failed_read(caplog):
    first, second = rel("g/app", iid=1), rel("g/mod", iid=2)
    first.client.get_merge_request.side_effect = RuntimeError("boom")

    await aensure_related_links([first, second])

    first.client.update_merge_request.assert_not_called()
    second.client.update_merge_request.assert_called_once()
    assert "Could not link g/app" in caplog.text
