"""Tests for BrokerClient against a real BrokerServer on localhost."""

import pytest
from pynet.loop import Event, sleep

from pymq.broker import (
    DeadReason,
    InvalidNameError,
    InvalidTopicError,
    QueueConfig,
    QueueFullError,
    QueueNotFoundError,
    TopicEvent,
)
from pymq.broker.client import BrokerClient, Delivery
from pymq.broker.server import BrokerServer
from tests.broker.net import HOST, WAIT, run_net, wait_until

ACK_TIMEOUT = 0.2
TOUCH_EVERY = 0.08
HOLD_FOR = 0.5
RETRY_DELAY = 0.01
MAX_ATTEMPTS = 2
PREFETCH = 3
JOBS = 5
SETTLE = 0.1
PRIORITY = 3
UNUSED_PORT = 9
STAT_POLL = 0.01


def connect(server: BrokerServer) -> BrokerClient:
    """Build a client for ``server`` with heartbeats off."""
    return BrokerClient(HOST, server.port, heartbeat_interval=None)


class TestQueues:
    """Check queue management calls."""

    def test_declare_list_purge_delete_and_stats(self) -> None:
        """Declare, list, put, purge, delete and read broker stats."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                config = QueueConfig(max_attempts=MAX_ATTEMPTS)
                assert await c.declare_queue("jobs", config) == config
                assert await c.declare_queue("other") == QueueConfig()
                assert await c.list_queues() == ["jobs", "other"]
                result = await c.put("jobs", {"n": 1}, id="m1", priority=PRIORITY)
                assert result.id == "m1"
                assert not result.duplicate
                assert (await c.put("jobs", {"n": 1}, id="m1")).duplicate
                assert (await c.stats("jobs")).ready == 1
                assert await c.purge_queue("jobs") == 1
                await c.delete_queue("other")
                assert await c.list_queues() == ["jobs"]
                stats = await c.broker_stats()
                assert stats["queues"] == 1
                assert stats["connections"] == 1

        run_net(main)

    def test_remote_errors_surface_as_broker_errors(self) -> None:
        """Raise QueueFullError and QueueNotFoundError from server replies."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("tiny", QueueConfig(max_length=1))
                await c.put("tiny", 1)
                with pytest.raises(QueueFullError):
                    await c.put("tiny", 2)
                with pytest.raises(QueueNotFoundError):
                    await c.stats("missing")

        run_net(main)

    def test_invalid_names_rejected_before_any_request(self) -> None:
        """Raise InvalidNameError/InvalidTopicError without touching the network."""

        async def main() -> None:
            # Never started: a request would raise NotConnectedError instead.
            c = BrokerClient(HOST, UNUSED_PORT)

            async def handler(_d: Delivery) -> None:
                """Do nothing."""

            async def on_event(_e: TopicEvent) -> None:
                """Do nothing."""

            for bad in ("jobs/../x", "", "a..b"):
                with pytest.raises(InvalidNameError):
                    await c.declare_queue(bad)
                with pytest.raises(InvalidNameError):
                    await c.put(bad, 1)
                with pytest.raises(InvalidNameError):
                    await c.consume(bad, handler)
                with pytest.raises(InvalidNameError):
                    await c.dead_letters(bad)
                with pytest.raises(InvalidNameError):
                    await c.bind("a.b", bad)
            for bad in ("#.x", "", "a..b"):
                with pytest.raises(InvalidTopicError):
                    await c.publish(bad, 1)
            with pytest.raises(InvalidTopicError):
                await c.subscribe("a..b", on_event)
            with pytest.raises(InvalidTopicError):
                await c.unbind("a..b", "jobs")
            await c.close()

        run_net(main)


class TestConsume:
    """Check consumers, auto-ack, manual ack and leases."""

    def test_auto_ack_round_trip(self) -> None:
        """Run the handler, then ack the delivery automatically."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("jobs")
                got: list[Delivery] = []

                async def handler(d: Delivery) -> None:
                    """Record the delivery."""
                    got.append(d)

                await c.put("jobs", {"n": 1}, headers={"k": "v"}, attachments=(b"zip",))
                consumer = await c.consume("jobs", handler)
                assert consumer.queue == "jobs"
                assert consumer.tag is not None
                await wait_until(lambda: len(got) == 1)
                await wait_until_acked(c, "jobs", 1)
                (d,) = got
                assert (d.queue, d.body, d.attachments) == ("jobs", {"n": 1}, (b"zip",))
                assert d.headers == {"k": "v"}
                assert d.attempt == 1
                assert d.priority == 0
                assert d.id == d.envelope.id
                assert d.settled
                await consumer.cancel()
                assert (await c.stats("jobs")).consumers == 0

        run_net(main)

    def test_handler_error_nacks_then_dead_letters(self) -> None:
        """Redeliver after a raising handler, then dead-letter with the error text."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                config = QueueConfig(max_attempts=MAX_ATTEMPTS, retry_delay=RETRY_DELAY)
                await c.declare_queue("jobs", config)
                attempts: list[int] = []

                async def handler(d: Delivery) -> None:
                    """Fail every time."""
                    attempts.append(d.attempt)
                    text = "boom"
                    raise ValueError(text)

                put = await c.put("jobs", "x")
                consumer = await c.consume("jobs", handler)
                await wait_until_stat(c, "jobs", "dead", 1)
                assert attempts == [1, MAX_ATTEMPTS]
                (letter,) = await c.dead_letters("jobs")
                assert letter.error == "ValueError: boom"
                assert letter.reason is DeadReason.MAX_ATTEMPTS
                assert letter.envelope.id == put.id
                await consumer.cancel()
                assert await c.replay_dead("jobs", [put.id]) == 1
                assert (await c.stats("jobs")).ready == 1
                assert await c.purge_dead("jobs") == 0

        run_net(main)

    def test_manual_ack_and_handler_settled_first(self) -> None:
        """Send nothing automatically when auto_ack is off or the handler settled."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("manual")
                await c.declare_queue("reject", QueueConfig(max_attempts=MAX_ATTEMPTS))
                held: list[Delivery] = []

                async def keep(d: Delivery) -> None:
                    """Hold the delivery for a later ack."""
                    held.append(d)

                async def reject(d: Delivery) -> None:
                    """Reject the delivery without requeue."""
                    assert await d.nack(requeue=False, error="nope")

                await c.put("manual", 1)
                await c.put("reject", 2)
                await c.consume("manual", keep, auto_ack=False)
                await c.consume("reject", reject)
                await wait_until(lambda: len(held) == 1)
                await wait_until_stat(c, "reject", "dead", 1)
                await sleep(SETTLE)
                assert (await c.stats("manual")).in_flight == 1
                assert not held[0].settled
                assert await held[0].ack()
                assert not await held[0].ack()
                assert (await c.stats("manual")).acked == 1
                letters = await c.dead_letters("reject", limit=1)
                assert letters[0].reason is DeadReason.REJECTED
                assert letters[0].error == "nope"
                assert (await c.stats("reject")).acked == 0
                assert await c.purge_dead("reject") == 1

        run_net(main)

    def test_touch_keeps_lease_past_ack_timeout(self) -> None:
        """Renew a lease with touch so a slow handler still acks successfully."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("slow", QueueConfig(ack_timeout=ACK_TIMEOUT))
                results: list[bool] = []

                async def handler(d: Delivery) -> None:
                    """Work for longer than ack_timeout, touching as we go."""
                    waited = 0.0
                    while waited < HOLD_FOR:
                        await sleep(TOUCH_EVERY)
                        waited += TOUCH_EVERY
                        results.append(await d.touch())
                    results.append(await d.ack())

                await c.put("slow", 1)
                await c.consume("slow", handler, auto_ack=False)
                await wait_until_acked(c, "slow", 1)
                assert all(results)
                stats = await c.stats("slow")
                assert (stats.delivered, stats.retried) == (1, 0)

        run_net(main)

    def test_concurrent_handlers_up_to_prefetch(self) -> None:
        """Run exactly ``prefetch`` handlers at once."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("jobs")
                release = Event()
                running: list[int] = []
                peak: list[int] = [0]

                async def handler(d: Delivery) -> None:
                    """Block until released."""
                    running.append(d.attempt)
                    peak[0] = max(peak[0], len(running))
                    await release.wait()
                    running.pop()

                for n in range(JOBS):
                    await c.put("jobs", n)
                await c.consume("jobs", handler, prefetch=PREFETCH)
                await wait_until(lambda: len(running) == PREFETCH)
                await sleep(SETTLE)
                assert len(running) == PREFETCH
                release.set()
                await wait_until_acked(c, "jobs", JOBS)
                assert peak[0] == PREFETCH

        run_net(main)


class TestTopics:
    """Check publish, subscribe and bindings."""

    def test_subscribe_receives_events_in_order(self) -> None:
        """Deliver TopicEvents with headers and attachments, in arrival order."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                events: list[TopicEvent] = []

                async def on_event(e: TopicEvent) -> None:
                    """Record the event after a pause, so order matters."""
                    await sleep(0)
                    events.append(e)

                sub = await c.subscribe("job.*", on_event)
                assert sub.pattern == "job.*"
                for n in range(JOBS):
                    result = await c.publish(
                        "job.done", n, headers={"n": str(n)}, attachments=(b"a",)
                    )
                    assert result.subscribers == 1
                await wait_until(lambda: len(events) == JOBS)
                assert [e.body for e in events] == list(range(JOBS))
                assert events[0].headers == {"n": "0"}
                assert events[0].attachments == (b"a",)
                assert events[0].topic == "job.done"
                await sub.cancel()
                assert (await c.publish("job.done", 1)).subscribers == 0

        run_net(main)

    def test_bind_copies_events_into_queue(self) -> None:
        """Consume published events from a bound queue with a topic header."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server, connect(server) as c:
                await c.declare_queue("jobs")
                await c.bind("job.#", "jobs")
                got: list[Delivery] = []

                async def handler(d: Delivery) -> None:
                    """Record the delivery."""
                    got.append(d)

                await c.consume("jobs", handler)
                result = await c.publish("job.done", {"x": 1}, headers={"a": "b"})
                assert result.queues == 1
                await wait_until(lambda: len(got) == 1)
                assert got[0].headers == {"a": "b", "topic": "job.done"}
                await c.unbind("job.#", "jobs")
                assert (await c.publish("job.done", 1)).queues == 0

        run_net(main)


async def wait_until_stat(c: BrokerClient, queue: str, field: str, value: int) -> None:
    """Poll queue stats until ``field`` reaches ``value``."""
    for _ in range(int(WAIT / STAT_POLL)):
        if getattr(await c.stats(queue), field) == value:
            return
        await sleep(STAT_POLL)
    msg = f"{queue}.{field} never reached {value}"
    raise AssertionError(msg)


async def wait_until_acked(c: BrokerClient, queue: str, value: int) -> None:
    """Poll queue stats until ``acked`` reaches ``value``."""
    await wait_until_stat(c, queue, "acked", value)
