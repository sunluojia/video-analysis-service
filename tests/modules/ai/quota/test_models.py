from datetime import UTC, datetime, timedelta
from uuid import uuid4

from video_analysis.modules.ai.quota.models import AiPool, AiUsage
from video_analysis.modules.scheduling.task.models import TaskKind

LISTEN, RECOGNIZE, SUMMARIZE = TaskKind.LISTEN, TaskKind.RECOGNIZE, TaskKind.SUMMARIZE
NOW = datetime(2026, 11, 17, tzinfo=UTC)
POOL = AiPool(
    uuid4(), capacity=12, requests_per_minute=600, shares={LISTEN: 4, RECOGNIZE: 6, SUMMARIZE: 2}
)


def usage(running=None, waiting=(LISTEN, RECOGNIZE, SUMMARIZE), recent=0):
    return AiUsage(running or {}, recent, dict.fromkeys(waiting, NOW))


def test_kind_below_its_share_goes_first():
    running = {LISTEN: 2, RECOGNIZE: 6, SUMMARIZE: 2}
    assert POOL.choose(usage(running), NOW) == LISTEN


def test_higher_priority_wins_among_kinds_below_share():
    assert POOL.choose(usage({LISTEN: 1, RECOGNIZE: 1}), NOW) == SUMMARIZE


def test_idle_share_is_lent_out():
    running = {LISTEN: 4, RECOGNIZE: 6}
    assert POOL.choose(usage(running, waiting=(LISTEN, RECOGNIZE)), NOW) == RECOGNIZE


def test_arriving_summary_takes_back_its_share_from_the_next_free_slot():
    running = {LISTEN: 5, RECOGNIZE: 6}
    assert POOL.choose(usage(running), NOW) == SUMMARIZE


def test_long_waiting_listen_outranks_recognition_when_borrowing():
    running = {LISTEN: 4, RECOGNIZE: 6}
    waiting = AiUsage(running, 0, {LISTEN: NOW - timedelta(minutes=30), RECOGNIZE: NOW})
    assert POOL.choose(waiting, NOW) == LISTEN


def test_nothing_is_dispatched_when_capacity_or_rate_is_used_up():
    assert POOL.choose(usage({LISTEN: 6, RECOGNIZE: 6}), NOW) is None
    assert POOL.choose(usage(recent=600), NOW) is None


def test_kinds_routed_to_other_models_are_ignored():
    pool = AiPool(uuid4(), 4, 60, {LISTEN: 4})
    assert pool.choose(usage(waiting=(RECOGNIZE,)), NOW) is None
