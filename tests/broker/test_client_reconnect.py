"""Tests for BrokerClient reconnection: replay, stale acks and put resends."""

import logging

import pytest
from pynet.loop import Event, get_running_loop, sleep, spawn
from pynet.messaging import Handler, Message, MessageType, Peer, Reply

from pymq.broker import (
    Broker,
    BrokerUnavailableError,
    Envelope,
    JsonValue,
    QueueConfig,
    TopicEvent,
)
from pymq.broker import protocol as p
from pymq.broker.client import BrokerClient, Delivery
from pymq.broker.protocol import Json
from pymq.broker.server import BrokerServer
from tests.broker.net import HOST, WAIT, run_net, wait_until

BACKOFF = 0.05
MAX_BACKOFF = 0.2
PUT_TIMEOUT = 0.5
REQUEST_TIMEOUT = 0.3
SLACK = 0.5
SECOND_ATTEMPT = 2
MAX_ATTEMPTS = 3
RETRY_DELAY = 0.01
STAMP = "2026-10-17T00:00:00"
ENVELOPE = Envelope("m1", "jobs", "x", {}, (), 0, 1, STAMP)


def client_for(port: int, **kwargs: float) -> BrokerClient:
    """Build a client that redials quickly and sends no heartbeats."""
    return BrokerClient(
        HOST,
        port,
        heartbeat_interval=None,
        initial_backoff=BACKOFF,
        max_backoff=MAX_BACKOFF,
        **kwargs,
    )


async def serve(broker: Broker | None = None, port: int = 0) -> BrokerServer:
    """Start a server for ``broker`` (a fresh one if None) on ``port``."""
    server = BrokerServer(broker, heartbeat_interval=None)
    await server.start(HOST, port)
    return server


async def drop(server: BrokerServer, client: BrokerClient) -> None:
    """Close ``server`` and wait until ``client`` notices the line is down."""
    await server.close()
    await wait_until(lambda: not client.connected)


async def ignore(_item: object) -> None:
    """Do nothing with a delivery or event."""


class CountingServer(BrokerServer):
    """A server that counts the settle requests it receives."""

    settles: int = 0

    def _setup(self, peer: Peer) -> None:
        """Wrap ack, nack and touch so each one is counted."""
        super()._setup(peer)
        for method in (p.MSG_ACK, p.MSG_NACK, p.MSG_TOUCH):
            peer.on(method, self._counted(peer._handlers[method]))

    def _counted(self, real: Handler) -> Handler:
        """Return ``real`` wrapped with a counter."""

        async def handler(msg: Message) -> JsonValue | Reply:
            """Count, then run the real handler."""
            CountingServer.settles += 1
            return await real(msg)

        return handler


class DropAfterPutServer(BrokerServer):
    """A server that stores the first put, then hangs up instead of replying."""

    dropped: bool = False

    def _setup(self, peer: Peer) -> None:
        """Wrap msg.put so its first call drops the connection after storing."""
        super()._setup(peer)
        real = peer._handlers[p.MSG_PUT]

        async def handler(msg: Message) -> JsonValue | Reply:
            """Run the real put; the first time, close the peer before replying."""
            reply = await real(msg)
            if not DropAfterPutServer.dropped:
                DropAfterPutServer.dropped = True
                spawn(peer.close())
                await Event().wait()  # cancelled by the close; no reply goes out
            return reply

        peer.on(p.MSG_PUT, handler)


class TestReplay:
    """Check that the client sets everything up again after reconnecting."""

    def test_consumer_resumes_after_server_restart(self) -> None:
        """Re-declare the queue on a new broker and resume consuming with a new tag."""

        async def main() -> None:
            server = await serve()
            port = server.port
            c = client_for(port)
            async with c:
                got: list[JsonValue] = []

                async def handler(d: Delivery) -> None:
                    """Record the body."""
                    got.append(d.body)

                await c.declare_queue("jobs", QueueConfig(max_attempts=MAX_ATTEMPTS))
                consumer = await c.consume("jobs", handler)
                await c.put("jobs", 1)
                await wait_until(lambda: got == [1])
                old_tag = consumer.tag
                await drop(server, c)
                fresh = Broker()
                fresh.declare_queue("other")
                fresh.consume("other", lambda _m: None)  # take ctag-1 on the new broker
                server = await serve(fresh, port)
                await c.wait_connected(WAIT)
                assert consumer.tag is not None
                assert consumer.tag != old_tag
                assert (await c.stats("jobs")).consumers == 1
                config = QueueConfig(max_attempts=MAX_ATTEMPTS)
                assert fresh.declare_queue("jobs", config) == config  # same config: no conflict
                await c.put("jobs", 2)
                await wait_until(lambda: got == [1, 2])
            await server.close()
            fresh.close()

        run_net(main)

    def test_subscription_and_binding_replayed(self) -> None:
        """Re-create the binding and the subscription on a new broker."""

        async def main() -> None:
            server = await serve()
            port = server.port
            async with client_for(port) as c:
                events: list[JsonValue] = []

                async def on_event(e: TopicEvent) -> None:
                    """Record the body."""
                    events.append(e.body)

                await c.declare_queue("inbox")
                await c.bind("news.*", "inbox")
                sub = await c.subscribe("news.*", on_event)
                await drop(server, c)
                server = await serve(port=port)
                await c.wait_connected(WAIT)
                assert sub.sub_id is not None
                await c.publish("news.today", "hello")
                await wait_until(lambda: events == ["hello"])
                assert (await c.stats("inbox")).ready == 1
            await server.close()

        run_net(main)

    def test_cancelled_and_deleted_are_not_replayed(self) -> None:
        """Leave out consumers, subscriptions and queues that were removed."""

        async def main() -> None:
            server = await serve()
            port = server.port
            async with client_for(port) as c:
                await c.declare_queue("keep")
                await c.declare_queue("gone")
                await c.bind("a.b", "gone")
                kept = await c.consume("keep", ignore)
                await c.consume("gone", ignore)
                consumer = await c.consume("keep", ignore)
                sub = await c.subscribe("a.b", ignore)
                await c.delete_queue("gone")
                await drop(server, c)
                await consumer.cancel()  # while disconnected: only forgotten locally
                await sub.cancel()
                fresh = Broker()
                server = await serve(fresh, port)
                await c.wait_connected(WAIT)
                assert fresh.list_queues() == ["keep"]
                assert (await c.stats("keep")).consumers == 1
                assert kept.tag is not None
                assert consumer.tag is None
                stats = fresh.broker_stats()
                assert stats.subscriptions == 0
                assert stats.bindings == 0
            await server.close()
            fresh.close()

        run_net(main)

    def test_failed_replay_step_is_logged_and_rest_continue(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log a declare that the new broker refuses and still start the consumer."""

        async def main() -> None:
            server = await serve()
            port = server.port
            async with client_for(port) as c:
                await c.declare_queue("jobs", QueueConfig(max_attempts=MAX_ATTEMPTS))
                consumer = await c.consume("jobs", ignore)
                await drop(server, c)
                fresh = Broker()
                fresh.declare_queue("jobs")  # a different config: the replayed declare fails
                server = await serve(fresh, port)
                await c.wait_connected(WAIT)
                assert consumer.tag is not None
                assert (await c.stats("jobs")).consumers == 1
            await server.close()
            fresh.close()

        with caplog.at_level(logging.WARNING, logger="pymq.broker"):
            run_net(main)
        assert "replay" in caplog.text
        assert "jobs" in caplog.text


class TestStaleDeliveries:
    """Check deliveries from an older connection are stale (Review Focus 2)."""

    def test_ack_from_old_connection_is_stale(self) -> None:
        """Return False for an old delivery without a request; the new copy stays leased."""

        async def main() -> None:
            broker = Broker()
            server = CountingServer(broker, heartbeat_interval=None)
            await server.start(HOST, 0)
            port = server.port
            async with client_for(port) as c:
                held: list[Delivery] = []

                async def keep(d: Delivery) -> None:
                    """Hold the delivery."""
                    held.append(d)

                await c.declare_queue("jobs", QueueConfig(retry_delay=RETRY_DELAY))
                await c.consume("jobs", keep, auto_ack=False)
                await c.put("jobs", "homework")
                await wait_until(lambda: len(held) == 1)
                await drop(server, c)
                server = CountingServer(broker, heartbeat_interval=None)
                await server.start(HOST, port)
                await wait_until(lambda: len(held) == SECOND_ATTEMPT)
                old, new = held
                assert new.attempt == SECOND_ATTEMPT
                before = CountingServer.settles
                assert not await old.touch()
                assert not await old.ack()
                assert CountingServer.settles == before
                stats = await c.stats("jobs")
                assert stats.in_flight == 1
                assert stats.acked == 0
                assert await new.ack()
                assert (await c.stats("jobs")).acked == 1
            await server.close()
            broker.close()

        run_net(main)


class TestPutResend:
    """Check that put resends the same id after a drop (Review Focus 5)."""

    def test_put_resent_after_drop_is_not_duplicated(self) -> None:
        """Store exactly one copy when the reply to the first attempt is lost."""

        async def main() -> None:
            broker = Broker()
            DropAfterPutServer.dropped = False
            server = DropAfterPutServer(broker, heartbeat_interval=None)
            await server.start(HOST, 0)
            async with client_for(server.port) as c:
                await c.declare_queue("jobs")
                result = await c.put("jobs", {"task": "wash the car"})
                assert DropAfterPutServer.dropped
                assert len(result.id) > 0
                stats = await c.stats("jobs")
                assert stats.ready == 1
                assert stats.duplicates == 1
            await server.close()
            broker.close()

        run_net(main)

    def test_put_gives_up_after_put_timeout(self) -> None:
        """Raise BrokerUnavailableError once put_timeout has passed with no broker."""

        async def main() -> None:
            server = await serve()
            async with client_for(server.port, put_timeout=PUT_TIMEOUT) as c:
                await c.declare_queue("jobs")
                await drop(server, c)
                clock = get_running_loop()
                start = clock.time()
                with pytest.raises(BrokerUnavailableError):
                    await c.put("jobs", 1)
                elapsed = clock.time() - start
                assert PUT_TIMEOUT <= elapsed < PUT_TIMEOUT + SLACK

        run_net(main)


class TestDisconnectedRequests:
    """Check requests made while the line is down."""

    def test_request_while_disconnected_raises_unavailable(self) -> None:
        """Wait up to request_timeout, then raise BrokerUnavailableError."""

        async def main() -> None:
            server = await serve()
            async with client_for(server.port, request_timeout=REQUEST_TIMEOUT) as c:
                await drop(server, c)
                clock = get_running_loop()
                start = clock.time()
                with pytest.raises(BrokerUnavailableError):
                    await c.list_queues()
                elapsed = clock.time() - start
                assert REQUEST_TIMEOUT <= elapsed < REQUEST_TIMEOUT + SLACK

        run_net(main)

    def test_request_waits_for_reconnection(self) -> None:
        """Answer a request made while disconnected once the broker is back."""

        async def main() -> None:
            server = await serve()
            port = server.port
            async with client_for(port) as c:
                await c.declare_queue("jobs")
                await drop(server, c)
                pending = spawn(c.list_queues())
                server = await serve(port=port)
                assert await pending == ["jobs"]
            await server.close()

        run_net(main)


class TestOrphansAndClose:
    """Check orphan consumers are cancelled and nothing starts after close."""

    def test_orphan_consumer_is_cancelled(self) -> None:
        """Cancel a server consumer the client does not know, freeing its message."""

        async def main() -> None:
            async with await serve() as server, client_for(server.port) as c:
                await c.declare_queue("jobs")
                await c.put("jobs", 1)
                await c._rc.request(p.CONSUME_START, {"queue": "jobs"})  # unknown to the client
                await wait_until(lambda: server.broker.stats("jobs").consumers == 0)
                stats = await c.stats("jobs")
                assert stats.in_flight == 0
                assert stats.ready + stats.delayed == 1  # given back to the queue

        run_net(main)

    def test_no_handler_starts_after_close_begins(self) -> None:
        """Drop deliveries and events that land once close has started."""

        async def main() -> None:
            async with await serve() as server:
                c = client_for(server.port)
                await c.start()
                await c.wait_connected(WAIT)
                calls: list[str] = []

                async def handler(_d: Delivery) -> None:
                    """Record the call."""
                    calls.append("delivery")

                async def on_event(_e: TopicEvent) -> None:
                    """Record the call."""
                    calls.append("event")

                await c.declare_queue("jobs")
                consumer = await c.consume("jobs", handler)
                sub = await c.subscribe("a.b", on_event)
                generation = c._router.generation
                c._router.stop()  # what close() does first
                body: Json = {
                    "consumer_tag": consumer.tag or "",
                    "delivery_tag": 1,
                    "envelope": p.envelope_to_json(ENVELOPE),
                }
                await c._router.on_deliver(
                    Message(MessageType.PUSH, None, p.PUSH_DELIVER, body), generation
                )
                event = TopicEvent("a.b", 1, {}, (), STAMP)
                ev_body: Json = {"sub_id": sub.sub_id or "", "event": p.event_to_json(event)}
                await c._router.on_event(
                    Message(MessageType.PUSH, None, p.PUSH_EVENT, ev_body), generation
                )
                await sleep(0)
                await c.close()
                assert calls == []

        run_net(main)
