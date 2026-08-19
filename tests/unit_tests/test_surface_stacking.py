"""Repository-wide guard on what may enclose a floating surface.

Lives at the top of ``tests/unit_tests/`` rather than in an app package, for the reason
``test_css_animations`` and ``test_surface_contrast`` do: the surfaces are one family shared
across apps, and the bug is in the *ancestor chain* — the same picker ships into a dozen
pages, and each page wraps it in something different. A guard scoped to one app would leave
its siblings unwatched.

``opacity``, ``transform`` (and its individual properties), ``filter`` and ``backdrop-filter``
each make the element carrying them a stacking context. A surface's ``z-index: 60`` then
stops outranking the scrim's 55 and the panel opens *under* the dim, and an ``opacity`` also
fades the panel's own background so the page shows through it. That is why the chat
composer's in-flight fade goes control by control instead of once on the form.

Animations are out of scope on purpose: ``test_css_animations`` already pins every keyframe
that moves a transform to ``backwards``, so the property is released the moment it ends and
no lasting stacking context survives.
"""

from __future__ import annotations

import re
from datetime import time

from django.urls import reverse

import pytest

from schedules.models import ScheduledJob
from tests.unit_tests.chat.chat_pages import create_session, render_new_chat, render_thread
from tests.unit_tests.htmltree import ElementStack
from tests.unit_tests.test_css_animations import CONTAINING_BLOCK_PROPERTIES
from tests.unit_tests.test_picker_popovers import INPUT_CSS, SURFACE_CONTAINERS
from tests.unit_tests.test_surface_contrast import iter_rules

# The containing-block half is shared with `test_css_animations`, so the two guards can't
# come to disagree about which properties do this. The word boundary is stricter here: that
# guard reads keyframe steps, this one whole rule bodies, where `--card-scale:` is a custom
# property and not a transform.
STACKING_PROPERTY = re.compile(
    r"(?<![-\w])(?:{})\s*:".format("|".join((*CONTAINING_BLOCK_PROPERTIES, "opacity", "filter", "backdrop-filter")))
)
STACKING_UTILITY = re.compile(r"^-?(?:opacity|scale|rotate|translate|skew|blur|backdrop-blur)-")
# A selector's subject reduced to the class it names: `.chat-composer--sending:hover` is the
# `chat-composer` element, and an element carries no modifier or state in its class list.
SUBJECT_CLASS = re.compile(r"^\.([\w-]+?)(?:--[\w-]+)?(?:[:\[].*)?$")
COMBINATOR = re.compile(r"[\s>+~]+")


class _SurfaceAncestors(ElementStack):
    """Collects the class tokens of every element that encloses a floating surface."""

    def __init__(self):
        super().__init__()
        self.tokens = set()
        self.surfaces = set()

    def visit(self, tag, classes, attrs):
        if found := SURFACE_CONTAINERS.intersection(classes):
            self.surfaces.update(found)
            self.tokens.update(token for _, frame in self.stack for token in frame)


def _enclosing_class(subject: str) -> str:
    """`.chat-composer--sending:hover` -> `chat-composer`; `` for anything but a bare class."""
    match = SUBJECT_CLASS.match(subject)
    return match.group(1) if match else ""


def _stacking_rules():
    """Every (subject class, rule) pair in the stylesheet whose rule makes its subject a
    stacking context."""
    for selector, body in iter_rules(INPUT_CSS):
        if not (STACKING_PROPERTY.search(body) or any(STACKING_UTILITY.match(u) for u in body.split())):
            continue
        for one in selector.split(","):
            if subject := _enclosing_class(COMBINATOR.split(one.strip())[-1]):
                yield subject, f"{selector.strip()} {{{body.strip()}}}"


def _a_schedule(user) -> ScheduledJob:
    """The schedules list only paints its row menu once it has a row."""
    job = ScheduledJob(
        user=user,
        name="Daily review",
        prompt="Review open merge requests.",
        repos=[{"repo_id": "owner/repo", "ref": ""}],
        frequency="daily",
        time=time(9, 0),
        is_enabled=True,
    )
    job.compute_next_run()
    job.save()
    return job


def _surface_hosting_pages(member_client, admin_client, member_user) -> dict[str, str]:
    """One rendered page per way the app wraps a surface — chat's dock, a list's filter bar,
    a form's pickers, a card's row menu, the configuration sidebar."""
    _a_schedule(member_user)
    return {
        "session_new_chat": render_new_chat(member_client),
        "session_detail": render_thread(member_client, create_session(member_user)),
        "session_list": member_client.get(reverse("session_list")).content.decode(),
        "schedule_create": member_client.get(reverse("schedule_create")).content.decode(),
        "schedule_list": member_client.get(reverse("schedule_list")).content.decode(),
        "site_configuration": admin_client.get(
            reverse("site_configuration", kwargs={"group_key": "agent"})
        ).content.decode(),
    }


@pytest.mark.django_db
def test_nothing_enclosing_a_surface_is_faded_or_transformed(member_client, admin_client, member_user):
    """The chat composer used to fade as one form while sending, and a sheet opened from its
    action row came up unreadable — below the scrim its own z-index outranks, with its
    background faded along with everything else in the subtree. Any of these properties on
    any ancestor of any surface is that same bug.
    """
    pages = _surface_hosting_pages(member_client, admin_client, member_user)
    enclosing = {}
    for name, html in pages.items():
        parser = _SurfaceAncestors()
        parser.feed(html)
        for token in parser.tokens:
            enclosing.setdefault(token, name)

    offenders = [
        f"{token!r} encloses a surface on {page} and carries a stacking utility"
        for token, page in sorted(enclosing.items())
        if STACKING_UTILITY.match(token)
    ]
    offenders += [
        f"{subject!r} encloses a surface on {enclosing[subject]}: {rule}"
        for subject, rule in _stacking_rules()
        if subject in enclosing
    ]

    assert not offenders, "A floating surface is trapped in a stacking context:\n" + "\n".join(offenders)


@pytest.mark.django_db
def test_the_guard_is_actually_looking_at_every_kind_of_surface(member_client, admin_client, member_user):
    """The assertion above can only catch a regression on a page it renders and a surface it
    recognises, so a surface that stops appearing has to fail here rather than quietly
    shrink the guard."""
    pages = _surface_hosting_pages(member_client, admin_client, member_user)
    seen = set()
    for html in pages.values():
        parser = _SurfaceAncestors()
        parser.feed(html)
        seen |= parser.surfaces

    assert seen == SURFACE_CONTAINERS, f"surfaces never rendered: {sorted(SURFACE_CONTAINERS - seen)}"
