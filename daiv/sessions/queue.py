from __future__ import annotations

from datetime import timedelta
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codebase.base import MergeRequestState
    from sessions.models import Run, RunEnvelope

# Passive-decay staleness threshold (AC4). A Queue row whose age (``finished_at or created_at``) is
# older than this surfaces a "stale · Nd" impact chip; a younger row carries no label. This is the
# SINGLE source of the threshold — the view imports it, never re-declaring "7 days". It flags the
# chip only; it NEVER excludes an item (membership stays Story 4.1's ``still_actionable`` job, AC6).
QUEUE_DECAY_STALE_AFTER = timedelta(days=7)


class QueueImpactClass(IntEnum):
    """The Needs-me Queue's impact-class ordinal (FR-11, AD-5) — most-urgent first.

    Ranks the four impact classes "by who or what is blocked, not by what failed loudest": a
    blocking item always outranks a non-blocking failure. Rank is the enum VALUE (0 = most urgent),
    so ``order_queue`` sorts on it directly. Presentation-only and NEVER persisted (AD-9 carries no
    rank field), so this is a plain ``IntEnum`` — no model field, migration, or ``CheckConstraint`` —
    mirroring the ``RunStatus.terminal()`` / ``MergeRequestState.resolved()`` classmethod idiom.

    v1 reaches only ``PASSIVE_DECAY`` in production: the top three classes need signals DAIV does not
    yet collect (reviewers/blocked-by, live pipeline status, a waiting-on-human run state), so
    :func:`impact_class` never emits them (NFR1 — no fabricated blocking signal). Their ranks are
    reserved so a deferred class lands by teaching :func:`impact_class` a new signal source, WITHOUT
    touching the view, the sort, or the template (AC5).
    """

    SOMEONE_ELSE_BLOCKED = 0  # DEFERRED (AD-5): a teammate is blocked on this (reviewers/blocked-by)
    MERGEABLE_BUT_BROKEN = 1  # DEFERRED (AD-5): an MR that would merge but its pipeline is red
    AGENT_IDLE = 2  # DEFERRED (FR-12): the agent is paused waiting on a human decision
    PASSIVE_DECAY = 3  # v1: an actionable item aging with nobody explicitly blocked

    @property
    def rank(self) -> int:
        """The ordinal sort key — lower is more urgent (the enum value, named for the sort site)."""
        return self.value

    @classmethod
    def ordered(cls) -> tuple[QueueImpactClass, ...]:
        """The classes most-urgent-first — the canonical impact sequence (mirrors ``RunStatus.terminal()``)."""
        return tuple(sorted(cls, key=lambda c: c.rank))


def impact_class(run: Run, envelope: RunEnvelope | None, mr_state: MergeRequestState | None = None) -> QueueImpactClass:
    """Map ONE still-actionable run to its impact class — pure, sync, read-only (AC5, AC8).

    The single place that turns signals into an impact class: adding a deferred class = adding a
    branch here fed by a new signal source, never editing the view, the sort, or the template (AC5).
    v1 substantiates only ``PASSIVE_DECAY`` — the three higher classes need signals DAIV does not
    collect, and emitting one without evidence would fabricate "someone is blocked" (NFR1). Each is
    documented below as an explicit deferred seam — a reserved enum rank (see :class:`QueueImpactClass`)
    plus the list below — rather than a dead ``if`` branch that lint/coverage would flag as unreachable,
    so the seam stays visible without dead code:

    - ``SOMEONE_ELSE_BLOCKED`` — DEFERRED (AD-5): needs reviewer / blocked-by data, via a new
      ``RepoClient`` fetch + a persistence/caching surface (never a live call from the view).
    - ``MERGEABLE_BUT_BROKEN`` — DEFERRED (AD-5): needs live pipeline / CI status (a new fetch).
    - ``AGENT_IDLE`` — DEFERRED (FR-12): needs a "waiting-on-human" run state ``RunStatus`` lacks.

    ``mr_state`` is accepted for the deferred ``MERGEABLE_BUT_BROKEN`` seam, which will read the batch
    MR state already attached to the item — never a new per-row live read (AC8). v1 ignores it.
    """
    # v1: no reviewer / pipeline / waiting-on-human signal is available, so every actionable item is
    # passive-decay and is ranked by age in ``order_queue``. Do NOT invent a higher class here (NFR1).
    return QueueImpactClass.PASSIVE_DECAY


def order_queue(items: list[dict]) -> list[dict]:
    """Re-sequence the Queue by impact, most-urgent first — pure, stable, read-only (AC1, AC4, AC8).

    A pure re-sequence of the item dicts Story 4.1's ``_build_queue_item`` produces (each carrying a
    pre-computed ``impact_class`` and an ``age_at`` = ``finished_at or created_at``): it changes ONLY
    the order — never membership or the count (AC6). Sorted by ``(impact_class.rank, age_at)``:
    impact class first (a blocking item above a non-blocking failure — never failures-first, AC1/AC3),
    then oldest-first within a class so the most-stale passive-decay item surfaces on top (AC4).

    Python's sort is stable, so items tied on ``(rank, age_at)`` keep Story 4.1's incoming
    ``("-created_at", "-id")`` order — deterministic across HTMX re-renders. Reads only the dict keys
    already in memory: no DB query and no live MR read (AC8). Returns a new list; the input is
    untouched.
    """
    return sorted(items, key=lambda it: (it["impact_class"].rank, it["age_at"]))
