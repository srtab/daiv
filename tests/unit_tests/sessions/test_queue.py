"""Story 4.2 — the pure impact-class ranking framework (``sessions/queue.py``).

Mirrors ``tests/unit_tests/sessions/test_reconcile.py``: a pure domain module, sync + read-only,
tested with synthetic inputs. The top three impact classes are production-unreachable in v1 (their
signals are deferred), so AC3 is proven at the **framework level** — constructing one item of each
class directly and asserting the ordinal sequence holds — while the v1 classifier maps every real
item to ``PASSIVE_DECAY`` (AC2, NFR1: no fabricated blocking signal).
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus
from sessions.queue import QUEUE_DECAY_STALE_AFTER, QueueImpactClass, impact_class, order_queue

from codebase.base import MergeRequestState

# The single live-read seam (mirrors test_reconcile / the console tests): ``order_queue`` must never
# reach it — ordering is pure over already-fetched data (AC8).
_LIVE_READ = "codebase.mr_state.get_merge_request_state"


def _item(impact: QueueImpactClass, age_at, run_id=None) -> dict:
    """A minimal Queue item carrying only the two ordering keys ``_build_queue_item`` attaches."""
    return {"run_id": run_id or uuid.uuid4(), "impact_class": impact, "age_at": age_at}


class TestQueueImpactClassOrdinal:
    """AC1: a first-class ordinal ranking, most-urgent first — never failures-first."""

    def test_ordered_is_the_canonical_most_urgent_first_sequence(self):
        assert QueueImpactClass.ordered() == (
            QueueImpactClass.SOMEONE_ELSE_BLOCKED,
            QueueImpactClass.MERGEABLE_BUT_BROKEN,
            QueueImpactClass.AGENT_IDLE,
            QueueImpactClass.PASSIVE_DECAY,
        )

    def test_rank_is_ascending_with_urgency(self):
        ranks = [c.rank for c in QueueImpactClass.ordered()]
        assert ranks == sorted(ranks)
        # The load-bearing invariant (FR-11): a blocking class outranks the passive-decay floor.
        assert QueueImpactClass.SOMEONE_ELSE_BLOCKED.rank < QueueImpactClass.PASSIVE_DECAY.rank


class TestOrderQueue:
    """AC1, AC3, AC4, AC8 — the pure re-sequence."""

    def test_blocking_class_ranks_above_an_older_passive_decay_failure(self):
        # AC3 (framework level): a blocking item outranks a non-blocking failure REGARDLESS of age —
        # a fresh blocking item still beats a 30-day-old failed run. Proves "not failures-first".
        now = timezone.now()
        old_failure = _item(QueueImpactClass.PASSIVE_DECAY, now - timedelta(days=30))
        fresh_blocking = _item(QueueImpactClass.SOMEONE_ELSE_BLOCKED, now)
        ordered = order_queue([old_failure, fresh_blocking])
        assert [it["run_id"] for it in ordered] == [fresh_blocking["run_id"], old_failure["run_id"]]

    def test_full_class_sequence_holds(self):
        # AC1/AC3: one item per class, shuffled in -> exact ordinal sequence out.
        now = timezone.now()
        items = [
            _item(QueueImpactClass.PASSIVE_DECAY, now),
            _item(QueueImpactClass.AGENT_IDLE, now),
            _item(QueueImpactClass.SOMEONE_ELSE_BLOCKED, now),
            _item(QueueImpactClass.MERGEABLE_BUT_BROKEN, now),
        ]
        assert [it["impact_class"] for it in order_queue(items)] == list(QueueImpactClass.ordered())

    def test_passive_decay_sorts_most_aged_first(self):
        # AC4: within a class, oldest (most-stale) on top — the v1 observable ordering.
        now = timezone.now()
        newer = _item(QueueImpactClass.PASSIVE_DECAY, now - timedelta(days=1))
        older = _item(QueueImpactClass.PASSIVE_DECAY, now - timedelta(days=10))
        ordered = order_queue([newer, older])
        assert [it["run_id"] for it in ordered] == [older["run_id"], newer["run_id"]]

    def test_sort_is_stable_for_ties(self):
        # Equal (rank, age) keep incoming order (Python's stable sort) — deterministic re-renders.
        now = timezone.now()
        a, b, c = (_item(QueueImpactClass.PASSIVE_DECAY, now) for _ in range(3))
        ordered = order_queue([a, b, c])
        assert [it["run_id"] for it in ordered] == [a["run_id"], b["run_id"], c["run_id"]]

    def test_order_queue_performs_no_live_read(self):
        # AC8: ordering is pure — it must not reach the live MR-state read.
        now = timezone.now()
        items = [_item(QueueImpactClass.PASSIVE_DECAY, now - timedelta(days=i)) for i in range(3)]
        with patch(_LIVE_READ) as read:
            order_queue(items)
        read.assert_not_called()

    def test_order_queue_does_not_mutate_its_input(self):
        now = timezone.now()
        items = [_item(QueueImpactClass.PASSIVE_DECAY, now - timedelta(days=i)) for i in range(3)]
        snapshot = list(items)
        order_queue(items)
        assert items == snapshot  # a new list is returned; the incoming order is untouched

    def test_empty_queue_orders_to_empty(self):
        assert order_queue([]) == []


class TestImpactClassV1:
    """AC2, NFR1 — v1 signals substantiate ONLY passive-decay; no fabricated top class."""

    def test_failed_run_is_passive_decay(self):
        assert impact_class(Run(status=RunStatus.FAILED), None) is QueueImpactClass.PASSIVE_DECAY

    def test_open_mr_run_is_passive_decay(self):
        run = Run(status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        env = RunEnvelope(status=EnvelopeStatus.NEEDS_ATTENTION)
        # Even handed the live MR state, v1 must not promote an open MR to a blocking class.
        assert impact_class(run, env, mr_state=MergeRequestState.OPEN) is QueueImpactClass.PASSIVE_DECAY

    def test_found_issues_run_is_passive_decay(self):
        run = Run(status=RunStatus.SUCCESSFUL)
        env = RunEnvelope(status=EnvelopeStatus.FOUND_ISSUES)
        assert impact_class(run, env) is QueueImpactClass.PASSIVE_DECAY


class TestStalenessThreshold:
    """AC4: the staleness threshold is a single module constant (the view imports it)."""

    def test_threshold_is_seven_days(self):
        assert timedelta(days=7) == QUEUE_DECAY_STALE_AFTER
