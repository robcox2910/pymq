"""Tests for the Broker core."""

import logging

import pytest

from pymq.broker import (
    Broker,
    InvalidNameError,
    InvalidTopicError,
    LeasedMessage,
    QueueConfig,
    QueueConfigConflictError,
    QueueFullError,
    QueueNotFoundError,
    TopicEvent,
)
from tests.broker.helpers import run_async

TWO = 2
THREE = 3


class TestQueues:
    """Check queue management."""

    def test_declare_idempotent_and_conflict(self) -> None:
        """Accept an equal redeclare and refuse a different one."""

        async def main() -> None:
            broker = Broker()
            cfg = QueueConfig(max_attempts=THREE)
            assert broker.declare_queue("jobs", cfg) == cfg
            assert broker.declare_queue("jobs", QueueConfig(max_attempts=THREE)) == cfg
            with pytest.raises(QueueConfigConflictError):
                broker.declare_queue("jobs")
            with pytest.raises(InvalidNameError):
                broker.declare_queue("jobs/../x")
            broker.declare_queue("a")
            assert broker.list_queues() == ["a", "jobs"]
            assert broker.broker_stats().queues == TWO

        run_async(main)

    def test_unknown_queue_errors(self) -> None:
        """Raise QueueNotFoundError from every queue method."""

        async def main() -> None:
            broker = Broker()
            calls = [
                lambda: broker.delete_queue("x"),
                lambda: broker.purge_queue("x"),
                lambda: broker.stats("x"),
                lambda: broker.put("x", 1),
                lambda: broker.consume("x", lambda _m: None),
                lambda: broker.ack("x", 1),
                lambda: broker.nack("x", 1),
                lambda: broker.touch("x", 1),
                lambda: broker.bind("#", "x"),
                lambda: broker.unbind("#", "x"),
                lambda: broker.dead_letters("x"),
                lambda: broker.replay_dead("x"),
                lambda: broker.purge_dead("x"),
            ]
            for call in calls:
                with pytest.raises(QueueNotFoundError):
                    call()

        run_async(main)

    def test_consumers_and_cancel(self) -> None:
        """Hand out broker-wide tags and cancel by tag alone."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("a")
            broker.declare_queue("b")
            got: list[LeasedMessage] = []
            assert broker.consume("a", got.append) == "ctag-1"
            tag = broker.consume("b", got.append)
            assert tag == "ctag-2"
            broker.put("b", "x")
            assert len(got) == 1
            assert broker.cancel(tag) == 1
            assert broker.cancel(tag) == 0
            assert broker.ack("b", got[0].delivery_tag) is False

        run_async(main)

    def test_delete_forgets_consumers_and_bindings(self) -> None:
        """Remove bindings and consumer tags with the queue."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q")
            tag = broker.consume("q", lambda _m: None)
            broker.bind("#", "q")
            broker.delete_queue("q")
            assert broker.broker_stats().bindings == 0
            assert broker.cancel(tag) == 0
            assert broker.publish("a", 1).queues == 0
            with pytest.raises(QueueNotFoundError):
                broker.stats("q")

        run_async(main)

    def test_close(self) -> None:
        """Forget everything on close."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q")
            broker.close()
            assert broker.list_queues() == []

        run_async(main)


class TestPublish:
    """Check topic publishing."""

    def test_fan_out(self) -> None:
        """Reach two subscribers and a bound queue."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q")
            broker.bind("news.*", "q")
            events: list[tuple[str, TopicEvent]] = []
            s1 = broker.subscribe("news.#", lambda sid, e: events.append((sid, e)))
            s2 = broker.subscribe("news.uk", lambda sid, e: events.append((sid, e)))
            result = broker.publish("news.uk", {"a": 1}, headers={"h": "v"}, attachments=(b"x",))
            assert (result.subscribers, result.queues) == (TWO, 1)
            assert [s for s, _ in events] == [s1, s2]
            assert events[0][1].attachments == (b"x",)
            assert broker.stats("q").ready == 1
            assert broker.unsubscribe(s1)
            assert broker.publish("news.uk", 1).subscribers == 1
            with pytest.raises(InvalidTopicError):
                broker.publish("#.x", 1)

        run_async(main)

    def test_bound_copy_has_topic_header(self) -> None:
        """Add the topic header to queued copies."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q")
            broker.bind("a.b", "q")
            broker.bind("a.b", "q")
            got: list[LeasedMessage] = []
            broker.consume("q", got.append)
            assert broker.publish("a.b", 1, headers={"h": "v"}).queues == 1
            assert dict(got[0].envelope.headers) == {"h": "v", "topic": "a.b"}
            assert broker.unbind("a.b", "q")

        run_async(main)

    def test_full_bound_queue_counts_drop(self) -> None:
        """Count a full bound queue instead of raising."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q", QueueConfig(max_length=1))
            broker.bind("#", "q")
            assert broker.publish("t", 1).queues == 1
            assert broker.publish("t", 2).queues == 0
            assert broker.broker_stats().binding_drops == 1
            with pytest.raises(QueueFullError):
                broker.put("q", 3)

        run_async(main)

    def test_raising_sink_does_not_stop_others(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log a failing sink and still call the rest."""

        async def main() -> None:
            broker = Broker()
            seen: list[str] = []

            def bad(_sid: str, _event: TopicEvent) -> None:
                msg = "boom"
                raise RuntimeError(msg)

            broker.subscribe("t", bad)
            broker.subscribe("t", lambda sid, _e: seen.append(sid))
            with caplog.at_level(logging.ERROR):
                assert broker.publish("t", 1).subscribers == TWO
            assert seen == ["sub-2"]
            assert "boom" in caplog.text

        run_async(main)


class TestDeadEvents:
    """Check the $sys.dead.<queue> events."""

    def test_rejected_nack_publishes_event(self) -> None:
        """Announce a rejected message with its reason."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("jobs")
            events: list[TopicEvent] = []
            broker.subscribe("$sys.dead.*", lambda _s, e: events.append(e))
            got: list[LeasedMessage] = []
            broker.consume("jobs", got.append)
            broker.put("jobs", "x", id="m1")
            assert broker.nack("jobs", got[0].delivery_tag, requeue=False, error="bad")
            assert len(events) == 1
            assert events[0].topic == "$sys.dead.jobs"
            body = events[0].body
            assert isinstance(body, dict)
            assert body["queue"] == "jobs"
            assert body["id"] == "m1"
            assert body["reason"] == "rejected"
            assert body["error"] == "bad"
            assert body["attempt"] == 1
            assert "dead_at" in body
            assert len(broker.dead_letters("jobs")) == 1
            assert broker.purge_dead("jobs") == 1
            assert broker.replay_dead("jobs") == 0

        run_async(main)

    def test_catch_all_binding_does_not_loop(self) -> None:
        """Land exactly one event copy in a queue bound to everything."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("q", QueueConfig(max_attempts=1))
            broker.bind("#", "q")
            got: list[LeasedMessage] = []
            broker.consume("q", got.append)
            broker.put("q", "x")
            assert broker.nack("q", got[0].delivery_tag)
            assert len(got) == TWO
            assert got[1].envelope.headers["topic"] == "$sys.dead.q"
            assert broker.stats("q").enqueued == TWO

        run_async(main)
