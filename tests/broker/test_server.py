"""Tests for the BrokerServer over real localhost sockets."""

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from pynet.errors import RemoteError
from pynet.framing import encode_frame
from pynet.loop import run, sleep, timeout
from pynet.messaging import Message, MessageType, Peer, connect, encode_message
from pynet.streams import open_connection

from pymq.broker import Broker, JsonValue, QueueConfig
from pymq.broker import protocol as p
from pymq.broker.server import BrokerServer

HOST = "127.0.0.1"
POLL = 0.005
WAIT = 2.0
LONG_WAIT = 8.0
TWO = 2
CONNECTIONS = 3
EVENTS = 2_000
JOBS = 10
STALLED_MAX_PENDING = 5
EVENT_BODY = "x" * 1_000
RETRY_DELAY = 0.01


def run_net(fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run a network test body on the real clock."""
    run(fn())


async def wait_until(check: Callable[[], bool], limit: float = WAIT) -> None:
    """Poll ``check`` until it is true, failing after ``limit`` seconds."""
    async with timeout(limit):
        while not check():
            await sleep(POLL)


class Client:
    """A raw pynet client that records the pushes it receives."""

    def __init__(self, peer: Peer, deliveries: list[Message], events: list[Message]) -> None:
        """Wrap a connected peer and its push lists."""
        self.peer = peer
        self.deliveries = deliveries
        self.events = events

    async def call(
        self, method: str, body: JsonValue = None, attachments: tuple[bytes, ...] = ()
    ) -> dict[str, JsonValue]:
        """Send a request and return its object body."""
        reply = await self.peer.request(method, body, attachments=attachments, timeout=WAIT)
        assert isinstance(reply.body, dict)
        return reply.body

    async def close(self) -> None:
        """Close the connection."""
        await self.peer.close()


async def client(port: int) -> Client:
    """Connect a recording client to the server on ``port``."""
    deliveries: list[Message] = []
    events: list[Message] = []

    async def on_deliver(msg: Message) -> JsonValue:
        """Record a delivery push."""
        deliveries.append(msg)
        return None

    async def on_event(msg: Message) -> JsonValue:
        """Record an event push."""
        events.append(msg)
        return None

    def setup(peer: Peer) -> None:
        """Register push handlers."""
        peer.on(p.PUSH_DELIVER, on_deliver)
        peer.on(p.PUSH_EVENT, on_event)

    peer = await connect(HOST, port, setup=setup, heartbeat_interval=None)
    return Client(peer, deliveries, events)


def _field(msg: Message, *path: str) -> JsonValue:
    """Return a nested field from a push body."""
    value: JsonValue = msg.body
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    return value


class TestRoundTrip:
    """Check the basic request handlers and the deliver push."""

    def test_declare_put_consume_ack(self) -> None:
        """Deliver a put message with attachments; ack once, then stale."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = await client(server.port)
                declared = await c.call(p.QUEUE_DECLARE, {"name": "jobs"})
                assert declared["config"] == p.config_to_json(QueueConfig())
                put = await c.call(
                    p.MSG_PUT, {"queue": "jobs", "body": {"n": 1}, "id": "m1"}, (b"zip",)
                )
                assert put == {"id": "m1", "duplicate": False}
                started = await c.call(p.CONSUME_START, {"queue": "jobs"})
                tag = started["consumer_tag"]
                await wait_until(lambda: len(c.deliveries) == 1)
                (d,) = c.deliveries
                assert _field(d, "consumer_tag") == tag
                assert _field(d, "envelope", "body") == {"n": 1}
                assert d.attachments == (b"zip",)
                ack = {
                    "queue": "jobs",
                    "consumer_tag": tag,
                    "delivery_tag": _field(d, "delivery_tag"),
                }
                assert await c.call(p.MSG_ACK, ack) == {"stale": False}
                assert await c.call(p.MSG_ACK, ack) == {"stale": True}
                stats = await c.call(p.QUEUE_STATS, {"name": "jobs"})
                assert stats["acked"] == 1
                await c.close()

        run_net(main)

    def test_management_requests(self) -> None:
        """Answer list, stats, bind, publish, purge and dead-letter requests."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = await client(server.port)
                await c.call(p.QUEUE_DECLARE, {"name": "a", "config": {"max_attempts": 1}})
                await c.call(p.QUEUE_DECLARE, {"name": "b"})
                assert await c.call(p.QUEUE_LIST) == {"queues": ["a", "b"]}
                assert await c.call(p.TOPIC_BIND, {"pattern": "job.#", "queue": "a"}) == {
                    "ok": True
                }
                published = await c.call(p.TOPIC_PUBLISH, {"topic": "job.x", "body": 1})
                assert published == {"subscribers": 0, "queues": 1}
                assert await c.call(p.QUEUE_PURGE, {"name": "a"}) == {"count": 1}
                assert await c.call(p.TOPIC_UNBIND, {"pattern": "job.#", "queue": "a"}) == {
                    "ok": True
                }
                await c.call(p.MSG_PUT, {"queue": "a", "body": "bad"})
                tag = (await c.call(p.CONSUME_START, {"queue": "a", "prefetch": 1}))["consumer_tag"]
                await wait_until(lambda: len(c.deliveries) == 1)
                nack = {
                    "queue": "a",
                    "consumer_tag": tag,
                    "delivery_tag": _field(c.deliveries[0], "delivery_tag"),
                    "requeue": False,
                    "error": "nope",
                }
                assert await c.call(p.MSG_NACK, nack) == {"stale": False}
                assert await c.call(p.MSG_TOUCH, nack) == {"stale": True}
                assert await c.call(p.CONSUME_CANCEL, {"consumer_tag": tag}) == {"ok": True}
                letters = await c.call(p.DEAD_LIST, {"queue": "a", "limit": 5})
                assert isinstance(letters["letters"], list)
                (letter,) = letters["letters"]
                assert p.dead_letter_from_json(letter).error == "nope"
                assert await c.call(p.DEAD_REPLAY, {"queue": "a", "ids": ["nope"]}) == {"count": 0}
                assert await c.call(p.DEAD_REPLAY, {"queue": "a"}) == {"count": 1}
                assert await c.call(p.DEAD_PURGE, {"queue": "a"}) == {"count": 0}
                assert await c.call(p.QUEUE_DELETE, {"name": "b"}) == {"ok": True}
                stats = await c.call(p.BROKER_STATS)
                assert stats["queues"] == 1
                assert stats["connections"] == 1
                assert stats["event_drops"] == 0
                await c.close()

        run_net(main)

    def test_subscribe_and_unsubscribe(self) -> None:
        """Push events to a subscriber until it unsubscribes."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = await client(server.port)
                sub = await c.call(p.TOPIC_SUBSCRIBE, {"pattern": "a.*"})
                sub_id = sub["sub_id"]
                body: JsonValue = {"topic": "a.b", "body": "hi", "headers": {"k": "v"}}
                assert (await c.call(p.TOPIC_PUBLISH, body, (b"pic",)))["subscribers"] == 1
                await wait_until(lambda: len(c.events) == 1)
                (e,) = c.events
                assert _field(e, "sub_id") == sub_id
                assert _field(e, "event", "body") == "hi"
                assert e.attachments == (b"pic",)
                assert await c.call(p.TOPIC_UNSUBSCRIBE, {"sub_id": sub_id}) == {"ok": True}
                assert (await c.call(p.TOPIC_PUBLISH, body))["subscribers"] == 0
                await c.close()

        run_net(main)


class TestErrors:
    """Check errors come back as RemoteError codes."""

    @pytest.mark.parametrize(
        ("method", "body", "code"),
        [
            (p.MSG_PUT, {"queue": "missing", "body": 1}, "queue_not_found"),
            (p.QUEUE_STATS, {"queue": 5}, "invalid_request"),
            (p.MSG_PUT, {"queue": 5, "body": 1}, "invalid_request"),
            (p.QUEUE_DECLARE, {"name": "jobs/../x"}, "invalid_name"),
            (p.QUEUE_DECLARE, {"name": ""}, "invalid_name"),
            (p.QUEUE_DECLARE, {"name": "a..b"}, "invalid_name"),
            (p.TOPIC_PUBLISH, {"topic": "#.x", "body": 1}, "invalid_topic"),
            (p.TOPIC_SUBSCRIBE, {"pattern": "a..b"}, "invalid_topic"),
            (p.TOPIC_SUBSCRIBE, {"pattern": "#", "max_pending": 0}, "invalid_request"),
            (p.QUEUE_DECLARE, {"name": "q", "config": {"max_attempts": 0}}, "invalid_request"),
            (p.QUEUE_LIST, [1], "invalid_request"),
        ],
    )
    def test_bad_requests(self, method: str, body: JsonValue, code: str) -> None:
        """Answer a bad request with the matching error code, never a crash."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = await client(server.port)
                with pytest.raises(RemoteError) as info:
                    await c.call(method, body)
                assert info.value.code == code
                assert await c.call(p.QUEUE_LIST) == {"queues": []}
                await c.close()

        run_net(main)

    def test_prefetch_below_one_is_invalid(self) -> None:
        """Refuse a consumer with prefetch 0 as invalid_request."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = await client(server.port)
                await c.call(p.QUEUE_DECLARE, {"name": "jobs"})
                with pytest.raises(RemoteError) as info:
                    await c.call(p.CONSUME_START, {"queue": "jobs", "prefetch": 0})
                assert info.value.code == "invalid_request"
                await c.close()

        run_net(main)


class TestOwnership:
    """Check each connection only controls its own consumers and subscriptions."""

    def test_disconnect_requeues_to_another_connection(self) -> None:
        """Requeue a leased message as attempt 2 when its consumer disconnects."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                first = await client(server.port)
                second = await client(server.port)
                await first.call(
                    p.QUEUE_DECLARE, {"name": "jobs", "config": {"retry_delay": RETRY_DELAY}}
                )
                await first.call(p.CONSUME_START, {"queue": "jobs", "prefetch": 1})
                await first.call(p.MSG_PUT, {"queue": "jobs", "body": "task"})
                await wait_until(lambda: len(first.deliveries) == 1)
                await second.call(p.CONSUME_START, {"queue": "jobs"})
                assert server.connection_count == TWO
                await first.close()
                await wait_until(lambda: len(second.deliveries) == 1)
                assert _field(second.deliveries[0], "envelope", "attempt") == TWO
                await wait_until(lambda: server.connection_count == 1)
                await second.close()

        run_net(main)

    def test_ack_from_non_owner_is_stale(self) -> None:
        """Ignore acks and cancels for a consumer another connection owns."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                owner = await client(server.port)
                other = await client(server.port)
                await owner.call(p.QUEUE_DECLARE, {"name": "jobs"})
                tag = (await owner.call(p.CONSUME_START, {"queue": "jobs"}))["consumer_tag"]
                sub_id = (await owner.call(p.TOPIC_SUBSCRIBE, {"pattern": "#"}))["sub_id"]
                await owner.call(p.MSG_PUT, {"queue": "jobs", "body": "task"})
                await wait_until(lambda: len(owner.deliveries) == 1)
                ack = {
                    "queue": "jobs",
                    "consumer_tag": tag,
                    "delivery_tag": _field(owner.deliveries[0], "delivery_tag"),
                }
                assert await other.call(p.MSG_ACK, ack) == {"stale": True}
                assert await other.call(p.MSG_TOUCH, ack) == {"stale": True}
                assert await other.call(p.MSG_NACK, ack) == {"stale": True}
                assert await other.call(p.MSG_ACK, {**ack, "queue": "nope"}) == {"stale": True}
                await other.call(p.CONSUME_CANCEL, {"consumer_tag": tag})
                await other.call(p.TOPIC_UNSUBSCRIBE, {"sub_id": sub_id})
                assert server.broker.stats("jobs").consumers == 1
                assert server.broker.broker_stats().subscriptions == 1
                assert await owner.call(p.MSG_ACK, ack) == {"stale": False}
                await owner.call(p.QUEUE_DELETE, {"name": "jobs"})
                assert await owner.call(p.MSG_ACK, ack) == {"stale": True}
                await owner.close()
                await wait_until(lambda: server.broker.broker_stats().subscriptions == 0)
                await other.close()

        run_net(main)


class TestDeliveryOwnership:
    """Check a connection cannot settle a delivery leased to another connection."""

    def test_ack_of_other_connections_delivery_is_stale(self) -> None:
        """Refuse an ack naming our own consumer but another connection's delivery."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                one = await client(server.port)
                two = await client(server.port)
                await one.call(p.QUEUE_DECLARE, {"name": "q"})
                start: JsonValue = {"queue": "q", "prefetch": 1}
                tag_one = (await one.call(p.CONSUME_START, start))["consumer_tag"]
                await wait_until(lambda: server.broker.stats("q").consumers == 1)
                tag_two = (await two.call(p.CONSUME_START, start))["consumer_tag"]
                await one.call(p.MSG_PUT, {"queue": "q", "body": "first"})
                await one.call(p.MSG_PUT, {"queue": "q", "body": "second"})
                await wait_until(lambda: len(one.deliveries) == 1 and len(two.deliveries) == 1)
                theirs = _field(two.deliveries[0], "delivery_tag")
                forged = {"queue": "q", "consumer_tag": tag_one, "delivery_tag": theirs}
                assert await one.call(p.MSG_ACK, forged) == {"stale": True}
                assert await one.call(p.MSG_NACK, forged) == {"stale": True}
                assert await one.call(p.MSG_TOUCH, forged) == {"stale": True}
                assert server.broker.stats("q").in_flight == TWO
                own = {"queue": "q", "consumer_tag": tag_two, "delivery_tag": theirs}
                assert await two.call(p.MSG_ACK, own) == {"stale": False}
                await one.close()
                await two.close()

        run_net(main)


class TestLifecycle:
    """Check starting, closing and broker ownership."""

    def test_close_keeps_a_passed_in_broker(self) -> None:
        """Close every connection but leave a caller's broker open."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("kept")
            server = BrokerServer(broker, heartbeat_interval=None)
            assert server.broker is broker
            await server.start()
            c = await client(server.port)
            await wait_until(lambda: server.connection_count == 1)
            await server.close()
            await c.peer.wait_closed()
            assert server.connection_count == 0
            assert broker.list_queues() == ["kept"]
            await server.close()  # closing twice is harmless

        run_net(main)

    def test_port_before_start_raises(self) -> None:
        """Refuse to report a port before the server is listening."""

        async def main() -> None:
            server = BrokerServer()
            with pytest.raises(RuntimeError):
                _ = server.port
            await server.close()

        run_net(main)


class TestSlowSubscribers:
    """Check one stalled subscriber cannot hold up anyone else (Review Focus 3)."""

    def test_stalled_subscriber_does_not_block_others(self) -> None:
        """Deliver everything to a fast client while a stalled one drops events."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                # S: a raw socket that subscribes to everything, then never reads.
                _reader, writer = await open_connection(HOST, server.port)
                body: JsonValue = {"pattern": "#", "max_pending": STALLED_MAX_PENDING}
                request = Message(MessageType.REQUEST, 1, p.TOPIC_SUBSCRIBE, body)
                for frame in encode_message(request):
                    writer.write(encode_frame(frame))
                await writer.drain()
                await wait_until(lambda: server.broker.broker_stats().subscriptions == 1)

                fast = await client(server.port)
                await fast.call(p.TOPIC_SUBSCRIBE, {"pattern": "#", "max_pending": EVENTS})
                await fast.call(p.QUEUE_DECLARE, {"name": "jobs"})
                await fast.call(p.CONSUME_START, {"queue": "jobs", "prefetch": JOBS})

                producer = await client(server.port)
                for n in range(EVENTS):
                    await producer.call(
                        p.TOPIC_PUBLISH, {"topic": "news.daily", "body": EVENT_BODY}
                    )
                    if n % (EVENTS // JOBS) == 0:
                        await producer.call(p.MSG_PUT, {"queue": "jobs", "body": n})

                await wait_until(
                    lambda: len(fast.events) == EVENTS and len(fast.deliveries) == JOBS,
                    LONG_WAIT,
                )
                stats = await producer.call(p.BROKER_STATS)
                drops = stats["event_drops"]
                assert isinstance(drops, int)
                assert drops > 0
                assert stats["connections"] == CONNECTIONS
                writer.abort()
                await fast.close()
                await producer.close()

        run_net(main)
