"""Tests for WorkQueue storage: priority, delay, dedupe, TTL and dead letters."""

import logging
import math
from dataclasses import replace

import pytest
from pynet.loop import sleep

from pymq.broker import (
    DeadLetter,
    DeadReason,
    QueueConfig,
    QueueFullError,
    QueueNotFoundError,
)
from pymq.broker.workqueue import WorkQueue
from tests.broker.helpers import run_async

DELAY = 5.0
JUST_BEFORE = 4.9
WINDOW = 300.0
PAST_WINDOW = 301.0
TTL = 10.0
SMALL_LIMIT = 2
THREE = 3
TWO = 2


class TestOrdering:
    """Check the order messages become ready in."""

    def test_priority_then_fifo(self) -> None:
        """Deliver the lowest priority first and keep put order within a tie."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("a", id="a", priority=5)
            q.put("b", id="b", priority=1)
            q.put("c", id="c", priority=5)
            q.put("d", id="d", priority=1)
            assert q.ready_ids() == ["b", "d", "a", "c"]

        run_async(main)

    def test_put_assigns_id_and_stores_defaults(self) -> None:
        """Generate an id and report a fresh put as not a duplicate."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            result = q.put({"x": 1}, headers={"k": "v"}, attachments=[b"z"])
            assert not result.duplicate
            assert q.ready_ids() == [result.id]
            assert q.name == "q"
            assert q.config == QueueConfig()
            assert q.stats().enqueued == 1

        run_async(main)


class TestDelay:
    """Check delayed messages."""

    def test_delay_moves_to_ready_on_time(self) -> None:
        """Hold a delayed message until its due time."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("a", delay=DELAY)
            await sleep(JUST_BEFORE)
            assert (q.stats().delayed, q.stats().ready) == (1, 0)
            await sleep(DELAY - JUST_BEFORE)
            assert (q.stats().delayed, q.stats().ready) == (0, 1)

        run_async(main)

    def test_earlier_delay_put_later_fires_first(self) -> None:
        """Re-arm the timer when a sooner message arrives."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("late", id="late", delay=DELAY)
            q.put("soon", id="soon", delay=1.0)
            await sleep(1.0)
            assert q.ready_ids() == ["soon"]
            await sleep(DELAY)
            assert q.ready_ids() == ["soon", "late"]

        run_async(main)

    @pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
    def test_bad_delay_rejected(self, bad: float) -> None:
        """Reject negative and non-finite delays."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            with pytest.raises(ValueError, match="delay"):
                q.put("a", delay=bad)
            assert q.stats().enqueued == 0

        run_async(main)


class TestDedupe:
    """Check duplicate-id handling."""

    def test_duplicate_id_within_window_ignored(self) -> None:
        """Ignore a repeated id and count it."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("a", id="x")
            again = q.put("a", id="x")
            assert again.duplicate
            assert again.id == "x"
            s = q.stats()
            assert (s.ready, s.duplicates, s.enqueued) == (1, 1, 1)

        run_async(main)

    def test_duplicate_id_after_window_accepted(self) -> None:
        """Forget ids once the window passes."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(dedupe_window=WINDOW, message_ttl=None))
            q.put("a", id="x")
            q.purge()
            assert q.put("a", id="x").duplicate
            await sleep(PAST_WINDOW)
            assert not q.put("a", id="x").duplicate
            assert q.stats().ready == 1

        run_async(main)

    def test_id_still_queued_after_window_is_duplicate(self) -> None:
        """Never hold two live copies of one id."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(dedupe_window=WINDOW))
            q.put("a", id="x")
            await sleep(PAST_WINDOW)
            assert q.put("a", id="x").duplicate
            assert q.stats().ready == 1

        run_async(main)

    def test_duplicate_on_full_queue_does_not_raise(self) -> None:
        """Never raise QueueFullError for a duplicate."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(max_length=1))
            q.put("a", id="x")
            assert q.put("a", id="x").duplicate

        run_async(main)


class TestLimits:
    """Check the length limit."""

    def test_max_length_raises_queue_full(self) -> None:
        """Count delayed messages toward the limit."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(max_length=SMALL_LIMIT))
            q.put("a")
            q.put("b", delay=DELAY)
            with pytest.raises(QueueFullError):
                q.put("c")

        run_async(main)


class TestTtl:
    """Check message expiry."""

    def test_ttl_dead_letters_with_reason(self) -> None:
        """Dead-letter an expired message and tell on_dead once."""

        async def main() -> None:
            seen: list[DeadLetter] = []
            q = WorkQueue("q", QueueConfig(message_ttl=TTL), on_dead=seen.append)
            q.put("a", id="a")
            await sleep(TTL)
            letters = q.dead_letters()
            assert letters[0].reason is DeadReason.TTL_EXPIRED
            assert letters[0].envelope.id == "a"
            assert len(seen) == 1
            s = q.stats()
            assert (s.ready, s.dead, s.dead_lettered) == (0, 1, 1)

        run_async(main)

    def test_ttl_covers_delayed_messages(self) -> None:
        """Expire a delayed message that never becomes ready in time."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL))
            q.put("a", delay=TTL * TWO)
            await sleep(TTL)
            s = q.stats()
            assert (s.delayed, s.dead) == (0, 1)
            await sleep(TTL * TWO)
            assert q.stats().ready == 0

        run_async(main)

    def test_purged_message_does_not_expire(self) -> None:
        """Ignore the stale TTL entry of a purged message."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL))
            q.put("a")
            q.purge()
            await sleep(TTL * TWO)
            assert q.stats().dead == 0

        run_async(main)


class TestDeadLetters:
    """Check the dead-letter list."""

    def test_dead_letter_cap_drops_oldest(self) -> None:
        """Keep only the newest letters and count the drops."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL, max_dead_letters=SMALL_LIMIT))
            for name in ("a", "b", "c"):
                q.put(name, id=name)
                await sleep(1.0)
            await sleep(TTL)
            assert [d.envelope.id for d in q.dead_letters()] == ["b", "c"]
            assert q.stats().dead_dropped == 1
            assert [d.envelope.id for d in q.dead_letters(limit=1)] == ["b"]

        run_async(main)

    def test_replay_and_purge_dead(self) -> None:
        """Put dead letters back, skipping unknown ids, then purge the rest."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL, max_length=1))
            q.put("a", id="a")
            await sleep(TTL)
            q.put("c", id="c")
            await sleep(TTL)
            dead_ids = [d.envelope.id for d in q.dead_letters()]
            assert dead_ids == ["a", "c"]
            assert q.replay_dead(["a", "nope"]) == 1
            assert q.ready_ids() == ["a"]
            assert q.replay_dead() == 1
            assert q.ready_ids() == ["a", "c"]  # ignores max_length
            assert q.dead_letters() == []
            await sleep(TTL)
            assert q.purge_dead() == TWO
            assert q.stats().dead == 0

        run_async(main)

    def test_replay_resets_attempt(self) -> None:
        """Reset the attempt count so the next delivery is the first."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("a", id="a")
            envelope = q._records["a"].envelope  # pyright: ignore[reportPrivateUsage]
            q._dead(replace(envelope, attempt=THREE), DeadReason.REJECTED, "boom")  # pyright: ignore[reportPrivateUsage]
            q.purge()
            assert q.dead_letters()[0].error == "boom"
            assert q.replay_dead() == 1
            assert q._records["a"].envelope.attempt == 0  # pyright: ignore[reportPrivateUsage]

        run_async(main)

    def test_on_dead_exception_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log and carry on when on_dead raises."""

        def boom(_letter: DeadLetter) -> None:
            msg = "listener broke"
            raise RuntimeError(msg)

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL), on_dead=boom)
            q.put("a")
            q.put("b")
            with caplog.at_level(logging.ERROR):
                await sleep(TTL)
            assert q.stats().dead == TWO

        run_async(main)
        assert "listener broke" in caplog.text


class TestHousekeeping:
    """Check purge, stats and close."""

    def test_purge_ready(self) -> None:
        """Drop ready and delayed messages and return how many."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("a")
            q.put("b", delay=DELAY)
            assert q.purge() == TWO
            s = q.stats()
            assert (s.ready, s.delayed) == (0, 0)
            await sleep(DELAY * TWO)
            assert q.stats().ready == 0

        run_async(main)

    def test_oldest_ready_age(self) -> None:
        """Measure from when the oldest ready message became ready."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            assert q.stats().oldest_ready_age is None
            q.put("a", delay=DELAY)
            await sleep(DELAY)
            q.put("b")
            await sleep(THREE)
            assert q.stats().oldest_ready_age == pytest.approx(THREE)

        run_async(main)

    def test_close_cancels_timers_and_blocks_use(self) -> None:
        """Refuse calls after close and fire nothing."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=TTL))
            q.put("a", delay=DELAY)
            q.close()
            q.close()
            await sleep(TTL * TWO)
            with pytest.raises(QueueNotFoundError):
                q.put("b")
            with pytest.raises(QueueNotFoundError):
                q.stats()

        run_async(main)
