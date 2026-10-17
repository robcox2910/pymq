"""Tests for Delivery handles and the client's push routing edge cases."""

import logging

import pytest
from pynet.loop import Event, sleep, spawn
from pynet.messaging import ConnectionEvent, Handler, Message, MessageType, Peer, Reply

from pymq.broker import Envelope, JsonValue, QueueNotFoundError, TopicEvent
from pymq.broker import protocol as p
from pymq.broker.client import BrokerClient, Delivery
from pymq.broker.protocol import Json
from pymq.broker.server import BrokerServer
from tests.broker.helpers import run_async
from tests.broker.net import HOST, WAIT, run_net, wait_until

REPLY_DELAY = 0.1
JOBS = 4
GENERATION = 1
DELIVERY_TAG = 7
UNUSED_PORT = 9
RETRY = 0.05


def envelope() -> Envelope:
    """Build a small envelope."""
    return Envelope("m1", "jobs", {"n": 1}, {"k": "v"}, (b"a",), 2, 1, "2026-10-17T00:00:00")


class FakeSettle:
    """Record settle calls and answer with a fixed result."""

    def __init__(self, *, result: bool = True) -> None:
        """Remember the answer to give."""
        self.result = result
        self.calls: list[tuple[str, Json]] = []

    async def __call__(self, _delivery: Delivery, method: str, body: Json) -> bool:
        """Record one call."""
        self.calls.append((method, body))
        return self.result


def delivery(settle: FakeSettle) -> Delivery:
    """Build a delivery wired to ``settle``."""
    return Delivery(
        envelope(),
        consumer_tag="c1",
        delivery_tag=DELIVERY_TAG,
        generation=GENERATION,
        settle=settle,
    )


class TestDelivery:
    """Check Delivery properties and settle rules."""

    def test_properties(self) -> None:
        """Expose the envelope's fields."""
        d = delivery(FakeSettle())
        assert (d.queue, d.id, d.body, d.headers) == ("jobs", "m1", {"n": 1}, {"k": "v"})
        assert (d.attachments, d.priority, d.attempt) == ((b"a",), 2, 1)
        assert (d.consumer_tag, d.delivery_tag, d.generation) == ("c1", DELIVERY_TAG, GENERATION)
        assert not d.settled

    def test_second_settle_makes_no_call(self) -> None:
        """Return False for a second settle without calling the network."""

        async def main() -> None:
            settle = FakeSettle()
            d = delivery(settle)
            assert await d.touch()
            assert await d.nack(requeue=False, error="bad")
            assert d.settled
            assert not await d.ack()
            assert not await d.nack()
            assert not await d.touch()
            ids: Json = {"queue": "jobs", "consumer_tag": "c1", "delivery_tag": DELIVERY_TAG}
            assert settle.calls == [
                (p.MSG_TOUCH, ids),
                (p.MSG_NACK, {**ids, "requeue": False, "error": "bad"}),
            ]

        run_async(main)

    def test_stale_ack_returns_false(self) -> None:
        """Pass a stale answer back as False."""

        async def main() -> None:
            d = delivery(FakeSettle(result=False))
            assert not await d.ack()
            assert d.settled

        run_async(main)


def delayed(original: Handler) -> Handler:
    """Wrap a request handler so its reply goes out ``REPLY_DELAY`` late."""

    async def run(msg: Message) -> JsonValue | Reply:
        """Run the real handler, then wait before replying."""
        result = await original(msg)
        await sleep(REPLY_DELAY)
        return result

    return run


class SlowReplyServer(BrokerServer):
    """A server that answers consume.start and topic.subscribe late."""

    def _setup(self, peer: Peer) -> None:
        """Wrap the start handlers so their replies trail the pushes."""
        super()._setup(peer)
        for method in (p.CONSUME_START, p.TOPIC_SUBSCRIBE):
            peer.on(method, delayed(peer._handlers[method]))


class TestPushBeforeReply:
    """Check pushes that overtake their start reply are not lost."""

    def test_deliveries_before_consume_reply(self) -> None:
        """Buffer deliveries that arrive before the consumer tag is known."""

        async def main() -> None:
            async with (
                SlowReplyServer(heartbeat_interval=None) as server,
                BrokerClient(HOST, server.port, heartbeat_interval=None) as c,
            ):
                await c.declare_queue("jobs")
                for n in range(JOBS):
                    await c.put("jobs", n)
                got: list[JsonValue] = []

                async def handler(d: Delivery) -> None:
                    """Record the body."""
                    got.append(d.body)

                await c.consume("jobs", handler, prefetch=JOBS)
                await wait_until(lambda: len(got) == JOBS)
                assert sorted(got, key=str) == list(range(JOBS))
                await wait_until(lambda: c._router.early_deliveries.empty)

        run_net(main)

    def test_events_before_subscribe_reply(self) -> None:
        """Buffer events that arrive before the subscription id is known."""

        async def main() -> None:
            async with (
                SlowReplyServer(heartbeat_interval=None) as server,
                BrokerClient(HOST, server.port, heartbeat_interval=None) as c,
            ):
                got: list[JsonValue] = []

                async def on_event(e: TopicEvent) -> None:
                    """Record the body."""
                    got.append(e.body)

                async def publish_soon() -> None:
                    """Publish while the subscribe reply is still delayed."""
                    await wait_until(lambda: server.broker.broker_stats().subscriptions == 1)
                    for n in range(JOBS):
                        await c.publish("a.b", n)

                task = spawn(publish_soon())
                await c.subscribe("a.*", on_event)
                await task
                await wait_until(lambda: len(got) == JOBS)
                assert got == list(range(JOBS))

        run_net(main)


class TestRobustness:
    """Check the client survives odd pushes and handler failures."""

    def test_unknown_pushes_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Drop deliveries and events nobody is waiting for."""

        async def main() -> None:
            c = BrokerClient(HOST, UNUSED_PORT)
            body: Json = {
                "consumer_tag": "nobody",
                "delivery_tag": 1,
                "envelope": p.envelope_to_json(envelope()),
            }
            await c._router.on_deliver(
                Message(MessageType.PUSH, None, p.PUSH_DELIVER, body), GENERATION
            )
            event = TopicEvent("a.b", 1, {}, (), "2026-10-17T00:00:00")
            ev_body: Json = {"sub_id": "nobody", "event": p.event_to_json(event)}
            await c._router.on_event(Message(MessageType.PUSH, None, p.PUSH_EVENT, ev_body))
            await c.close()

        with caplog.at_level(logging.DEBUG, logger="pymq.broker"):
            run_net(main)
        assert "nobody" in caplog.text

    def test_handler_errors_are_logged_and_client_keeps_working(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log a failing event handler and keep delivering later events."""

        async def main() -> None:
            async with (
                BrokerServer(heartbeat_interval=None) as server,
                BrokerClient(HOST, server.port, heartbeat_interval=None) as c,
            ):
                got: list[JsonValue] = []

                async def on_event(e: TopicEvent) -> None:
                    """Fail on the first event only."""
                    if e.body == 0:
                        text = "first"
                        raise RuntimeError(text)
                    got.append(e.body)

                await c.subscribe("a.b", on_event)
                await c.publish("a.b", 0)
                await c.publish("a.b", 1)
                await wait_until(lambda: got == [1])

        with caplog.at_level(logging.ERROR, logger="pymq.broker"):
            run_net(main)
        assert "RuntimeError: first" in caplog.text

    def test_close_cancels_running_handlers(self) -> None:
        """Cancel handler tasks when the client closes."""

        async def main() -> None:
            async with BrokerServer(heartbeat_interval=None) as server:
                c = BrokerClient(HOST, server.port, heartbeat_interval=None)
                await c.start()
                await c.wait_connected(timeout=1.0)
                await c.declare_queue("jobs")
                started = Event()
                never = Event()
                finished: list[bool] = []

                async def handler(_d: Delivery) -> None:
                    """Block forever."""
                    started.set()
                    await never.wait()
                    finished.append(True)

                await c.put("jobs", 1)
                await c.consume("jobs", handler)
                await started.wait()
                await c.close()
                await c.close()
                assert not finished
                assert not c.connected

        run_net(main)


class TestConnectionLoss:
    """Check settling, cancelling and starting when the line is down."""

    def test_settle_and_cancel_after_server_goes_away(self) -> None:
        """Return False from ack and cancel quietly once the server has gone."""

        async def main() -> None:
            server = BrokerServer(heartbeat_interval=None)
            await server.start()
            seen: list[ConnectionEvent] = []
            c = BrokerClient(HOST, server.port, heartbeat_interval=None, initial_backoff=RETRY)
            c.on_event(seen.append)
            await c.start()
            await c.wait_connected(timeout=WAIT)
            await c.declare_queue("jobs")
            held: list[Delivery] = []

            async def keep(d: Delivery) -> None:
                """Hold the delivery."""
                held.append(d)

            async def on_event(_e: TopicEvent) -> None:
                """Do nothing."""

            await c.put("jobs", 1)
            consumer = await c.consume("jobs", keep, auto_ack=False)
            sub = await c.subscribe("a.b", on_event)
            await wait_until(lambda: len(held) == 1)
            await server.close()
            await wait_until(lambda: not c.connected)
            assert not await held[0].ack()
            await consumer.cancel()
            await sub.cancel()
            await consumer.cancel()
            await sub.cancel()
            assert consumer.tag is None
            assert sub.sub_id is None
            await c.close()
            assert seen[0] is ConnectionEvent.CONNECTED
            assert ConnectionEvent.DISCONNECTED in seen

        run_net(main)

    def test_enter_times_out_and_closes(self) -> None:
        """Raise TimeoutError from ``async with`` when nothing answers, then close."""

        async def main() -> None:
            c = BrokerClient(HOST, UNUSED_PORT, request_timeout=RETRY, initial_backoff=RETRY)
            with pytest.raises(TimeoutError):
                async with c:
                    pass
            assert not c.connected

        run_net(main)

    def test_failed_consume_start_leaves_nothing_held(self) -> None:
        """Surface QueueNotFoundError from consume and keep the buffer empty."""

        async def main() -> None:
            async with (
                BrokerServer(heartbeat_interval=None) as server,
                BrokerClient(HOST, server.port, heartbeat_interval=None) as c,
            ):

                async def handler(_d: Delivery) -> None:
                    """Do nothing."""

                with pytest.raises(QueueNotFoundError):
                    await c.consume("missing", handler)
                assert c._router.early_deliveries.empty

        run_net(main)

    def test_malformed_pushes_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log and drop pushes whose bodies are not objects."""

        async def main() -> None:
            c = BrokerClient(HOST, UNUSED_PORT)
            await c._router.on_deliver(Message(MessageType.PUSH, None, p.PUSH_DELIVER, 1), 1)
            await c._router.on_event(Message(MessageType.PUSH, None, p.PUSH_EVENT, [1]))
            await c.close()

        with caplog.at_level(logging.WARNING, logger="pymq.broker"):
            run_net(main)
        assert "malformed delivery" in caplog.text
        assert "malformed event" in caplog.text
