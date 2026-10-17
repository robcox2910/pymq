"""Tests for WorkQueue consumers: dispatch, leases, ack/nack/touch and retries."""

import random

import pytest
from pynet.loop import sleep

from pymq.broker import DeadReason, LeasedMessage, QueueConfig
from pymq.broker.workqueue import WorkQueue
from tests.broker.helpers import run_async

FOUR = 4
THREE = 3
TWO = 2
PREFETCH = 2
FIVE = 5
ACK_TIMEOUT = 10.0
TOUCH_AT = 8.0
STEPS = 500
SEED = 1234
BIG_ATTEMPT = 5000
CAP = 60.0
FLOOR_TEST_PUTS = 300
P_PUT = 0.4
P_SETTLE = 0.8
P_ACK = 0.5
P_NACK = 0.8
P_REQUEUE = 0.7


class TestDispatch:
    """Check how ready messages reach consumers."""

    def test_round_robin_across_consumers(self) -> None:
        """Alternate between two consumers."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.add_consumer("B", got.append)
            for i in range(FOUR):
                q.put(i)
            assert [m.consumer_tag for m in got] == ["A", "B", "A", "B"]
            assert [m.delivery_tag for m in got] == [1, 2, THREE, FOUR]
            assert all(m.envelope.attempt == 1 for m in got)

        run_async(main)

    def test_backlog_goes_out_when_consumer_arrives(self) -> None:
        """Deliver waiting messages as soon as a consumer is added."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("x")
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            assert len(got) == 1
            assert q.stats().in_flight == 1
            assert q.stats().consumers == 1
            assert q.consumer_count == 1

        run_async(main)

    def test_prefetch_limits_in_flight(self) -> None:
        """Stop at the prefetch limit and resume after an ack."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append, prefetch=PREFETCH)
            for i in range(FIVE):
                q.put(i)
            assert len(got) == PREFETCH
            assert q.ack(got[0].delivery_tag)
            assert len(got) == THREE

        run_async(main)

    def test_bad_consumer_arguments(self) -> None:
        """Reject a zero prefetch and a duplicate tag."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            with pytest.raises(ValueError, match="prefetch"):
                q.add_consumer("A", lambda _m: None, prefetch=0)
            q.add_consumer("A", lambda _m: None)
            with pytest.raises(ValueError, match="duplicate"):
                q.add_consumer("A", lambda _m: None)

        run_async(main)

    def test_priority_order_is_delivery_order(self) -> None:
        """Deliver lower priority numbers first."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.put("lo", priority=5)
            q.put("hi", priority=1)
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            assert [m.envelope.body for m in got] == ["hi", "lo"]

        run_async(main)


class TestAck:
    """Check acknowledging."""

    def test_ack_removes_message(self) -> None:
        """Remove an acked message for good."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x")
            assert q.ack(got[0].delivery_tag) is True
            stats = q.stats()
            assert (stats.in_flight, stats.acked, stats.delivered) == (0, 1, 1)
            await sleep(1000)
            assert len(got) == 1

        run_async(main)

    def test_double_ack_is_stale(self) -> None:
        """Return False for a second ack and an unknown tag."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x")
            tag = got[0].delivery_tag
            assert q.ack(tag) is True
            assert q.ack(tag) is False
            assert q.nack(tag) is False
            assert q.touch(tag) is False
            assert q.ack(999) is False
            assert q.stats().acked == 1

        run_async(main)

    def test_leased_id_is_a_duplicate(self) -> None:
        """Treat a put of a currently leased id as a duplicate."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(dedupe_window=1.0))
            q.add_consumer("A", lambda _m: None)
            q.put("x", id="a")
            await sleep(5)
            assert q.put("x", id="a").duplicate is True
            assert q.stats().in_flight == 1

        run_async(main)


class TestRetries:
    """Check nack, backoff and dead lettering."""

    def test_nack_backs_off_then_dead_letters(self) -> None:
        """Redeliver after 1 s then 2 s, then dead-letter with the error."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(retry_delay=1.0, max_attempts=THREE))
            got: list[LeasedMessage] = []
            times: list[float] = []

            def sink(m: LeasedMessage) -> None:
                got.append(m)
                times.append(q._loop.time())  # pyright: ignore[reportPrivateUsage]

            q.add_consumer("A", sink)
            q.put("x", id="x")
            t0 = times[0]
            assert q.nack(got[0].delivery_tag)
            assert q.stats().delayed == 1
            await sleep(1.5)
            assert [m.envelope.attempt for m in got] == [1, 2]
            assert times[1] - t0 == pytest.approx(1.0)
            nacked_at = q._loop.time()  # pyright: ignore[reportPrivateUsage]
            assert q.nack(got[1].delivery_tag)
            await sleep(3)
            assert times[2] - nacked_at == pytest.approx(2.0)
            assert q.nack(got[2].delivery_tag, error="boom")
            letters = q.dead_letters()
            assert len(letters) == 1
            assert letters[0].reason is DeadReason.MAX_ATTEMPTS
            assert letters[0].error == "boom"
            assert q.stats().retried == TWO
            assert q.stats().in_flight == 0

        run_async(main)

    def test_nack_without_requeue_dead_letters_as_rejected(self) -> None:
        """Dead-letter a rejected message at once."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x")
            assert q.nack(got[0].delivery_tag, requeue=False, error="bad")
            letter = q.dead_letters()[0]
            assert letter.reason is DeadReason.REJECTED
            assert letter.error == "bad"
            assert letter.envelope.attempt == 1

        run_async(main)

    def test_replay_resets_attempt(self) -> None:
        """Redeliver a replayed dead letter as attempt 1."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x", id="x")
            q.nack(got[0].delivery_tag, requeue=False)
            assert q.replay_dead() == 1
            assert len(got) == TWO
            assert got[1].envelope.attempt == 1
            assert got[1].delivery_tag == TWO

        run_async(main)

    def test_replay_with_failing_sink_keeps_every_letter_accounted(self) -> None:
        """Survive dispatch dead-lettering while replay is still running."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(max_attempts=1))
            for i in range(THREE):
                q.put(i, id=str(i))
            q.add_consumer("A", lambda _m: None, prefetch=1)
            q.remove_consumer("A")
            assert q.stats().dead == 1
            q.add_consumer("B", _boom, prefetch=FIVE)
            assert q.stats().dead == THREE
            assert q.replay_dead() == THREE
            assert q.stats().dead == THREE
            assert q.stats().ready == 0

        run_async(main)


def test_replay_skips_second_dead_letter_with_same_id() -> None:
    """Replay one letter per id and leave the duplicate in the dead list."""

    async def main() -> None:
        q = WorkQueue("q", QueueConfig(dedupe_window=1.0))
        for _ in range(TWO):
            q.put("x", id="a")
            q.add_consumer("A", lambda _m: None)
            tag = next(iter(q._leases))  # pyright: ignore[reportPrivateUsage]
            q.nack(tag, requeue=False)
            q.remove_consumer("A")
            await sleep(5)
        assert q.stats().dead == TWO
        assert q.replay_dead() == 1
        stats = q.stats()
        assert (stats.dead, stats.ready + stats.in_flight) == (1, 0 + 1)

    run_async(main)


def _boom(_m: LeasedMessage) -> None:
    """Raise to simulate a broken sink."""
    msg = "sink broke"
    raise RuntimeError(msg)


class TestLeases:
    """Check lease expiry, touch and consumer removal."""

    def test_lease_expiry_redelivers_and_late_ack_is_stale(self) -> None:
        """Hand a hung consumer's message to another and refuse the late ack."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(ack_timeout=ACK_TIMEOUT, retry_delay=1.0))
            a: list[LeasedMessage] = []
            b: list[LeasedMessage] = []
            q.add_consumer("A", a.append)
            q.add_consumer("B", b.append)
            q.put("x", id="x")
            await sleep(ACK_TIMEOUT + 2.5)
            assert len(a) == 1
            assert [m.envelope.attempt for m in b] == [TWO]
            assert q.ack(a[0].delivery_tag) is False
            assert q.stats().in_flight == 1
            assert q.stats().acked == 0
            assert q.ack(b[0].delivery_tag) is True

        run_async(main)

    def test_touch_extends_lease(self) -> None:
        """Push the deadline out by a full ack_timeout from the touch."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(ack_timeout=ACK_TIMEOUT))
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x")
            await sleep(TOUCH_AT)
            assert q.touch(got[0].delivery_tag) is True
            await sleep(7)
            assert q.stats().in_flight == 1
            await sleep(3.5)
            assert q.stats().in_flight == 0
            assert q.stats().delayed == 1

        run_async(main)

    def test_remove_consumer_returns_leases_as_attempts(self) -> None:
        """Nack a removed consumer's leases and count the attempt."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            a: list[LeasedMessage] = []
            q.add_consumer("A", a.append)
            q.put(1)
            q.put(2)
            assert q.remove_consumer("A") == TWO
            assert q.remove_consumer("A") == 0
            assert q.stats().delayed == TWO
            assert q.stats().retried == TWO
            b: list[LeasedMessage] = []
            q.add_consumer("B", b.append)
            await sleep(2)
            assert [m.envelope.attempt for m in b] == [TWO, TWO]

        run_async(main)

    def test_remove_consumer_keeps_round_robin_fair(self) -> None:
        """Keep serving the remaining consumers in turn."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            for tag in ("A", "B", "C"):
                q.add_consumer(tag, got.append)
            q.put(1)
            q.put(2)
            q.remove_consumer("A")
            q.put(3)
            q.put(4)
            assert [m.consumer_tag for m in got[TWO:]] == ["C", "B"]

        run_async(main)

    def test_sink_exception_requeues(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log a failing sink and nack its message with a delay."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            q.add_consumer("A", _boom)
            q.put("x")
            stats = q.stats()
            assert (stats.in_flight, stats.delayed, stats.retried) == (0, 1, 1)

        with caplog.at_level("ERROR"):
            run_async(main)
        assert "sink" in caplog.text

    def test_sink_can_ack_synchronously(self) -> None:
        """Allow a sink to settle a message from inside delivery."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            seen: list[int] = []

            def sink(m: LeasedMessage) -> None:
                seen.append(m.delivery_tag)
                q.ack(m.delivery_tag)

            q.add_consumer("A", sink, prefetch=1)
            for i in range(THREE):
                q.put(i)
            assert seen == [1, TWO, THREE]
            assert q.stats().acked == THREE

        run_async(main)

    def test_close_drops_consumers_without_requeue(self) -> None:
        """Forget leases on close and refuse further settling."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append)
            q.put("x")
            q.close()
            assert q.consumer_count == 0
            assert q.ack(got[0].delivery_tag) is False
            await sleep(100)
            assert len(got) == 1

        run_async(main)


class TestMaintenance:
    """Check purge, stale heap entries and backoff arithmetic."""

    def test_purge_keeps_leased_messages(self) -> None:
        """Drop waiting messages but leave leases alone."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig())
            got: list[LeasedMessage] = []
            q.add_consumer("A", got.append, prefetch=1)
            q.put(1)
            q.put(2)
            assert q.purge() == 1
            assert q.stats().in_flight == 1
            assert q.ack(got[0].delivery_tag)

        run_async(main)

    def test_ready_heap_compacts_when_ttl_kills_ready(self) -> None:
        """Keep the ready heap small when expired messages pile up."""

        async def main() -> None:
            q = WorkQueue("q", QueueConfig(message_ttl=1.0))
            for i in range(FLOOR_TEST_PUTS):
                q.put(i)
            await sleep(2)
            assert q.stats().dead == FLOOR_TEST_PUTS
            assert len(q._ready) < FLOOR_TEST_PUTS // TWO  # pyright: ignore[reportPrivateUsage]

        run_async(main)

    def test_retry_wait_survives_huge_attempts(self) -> None:
        """Cap the backoff instead of overflowing."""
        assert QueueConfig().retry_wait(BIG_ATTEMPT) == CAP


class TestConservation:
    """Check nothing is lost or duplicated under random use."""

    def test_conservation_under_random_operations(self) -> None:
        """Account for every put id exactly once after random operations."""

        async def main() -> None:
            rng = random.Random(SEED)  # noqa: S311
            q = WorkQueue("q", QueueConfig(max_attempts=THREE, ack_timeout=5.0))
            live: dict[int, LeasedMessage] = {}
            acked: set[str] = set()

            def sink(m: LeasedMessage) -> None:
                live[m.delivery_tag] = m

            q.add_consumer("A", sink, prefetch=FIVE)
            q.add_consumer("B", sink, prefetch=FIVE)
            put: list[str] = []
            for step in range(STEPS):
                action = rng.random()
                if action < P_PUT:
                    mid = f"m{step}"
                    q.put(step, id=mid, delay=rng.choice([0.0, 0.0, 2.0]))
                    put.append(mid)
                elif action < P_SETTLE and live:
                    tag = rng.choice(sorted(live))
                    message = live.pop(tag)
                    choice = rng.random()
                    if choice < P_ACK:
                        if q.ack(tag):
                            acked.add(message.envelope.id)
                    elif choice < P_NACK:
                        q.nack(tag, requeue=rng.random() < P_REQUEUE)
                else:
                    await sleep(rng.choice([0.5, 3.0, 8.0]))
            dead = {d.envelope.id for d in q.dead_letters()}
            stats = q.stats()
            assert len(dead) == stats.dead
            assert (
                len(put) == len(acked) + stats.dead + stats.ready + stats.delayed + stats.in_flight
            )
            assert not acked & dead

        run_async(main)
